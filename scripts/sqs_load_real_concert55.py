#!/usr/bin/env python3
"""
콘서트 부하 입력기 v5.5 (Concert5.5)

목표
- 이 스크립트는 "연출용 제어"가 아니라, 실제 서버 로직(WR -> permit -> commit(hold) -> SQS)을 그대로 타며
  서버가 버티는지 / 얼마나 빨리 처리되는지 측정하기 위한 입력 도구다.
- 클라이언트가 불필요하게 WR/status를 과도 폴링해서 서버를 더 느리게 만드는 문제를 피하기 위해,
  status 폴링은 적응형 backoff(최대 간격 제한)으로 수행한다.

권장 사용(초반 enter burst + 이후 admit/commit 최대 처리)
  WRITE_API_BASE_URL="http://write-api.ticketing.svc.cluster.local:5001" \
  python3 scripts/sqs_load_real_concert55.py \
    --show-id 8 -n 30 \
    --enter-duration-sec 3 \
    --enter-concurrency 4000 \
    --commit-concurrency 3000 \
    --admit-timeout-sec 600 \
    --status-interval-ms 100 --status-interval-max-ms 2000 \
    --commit-retries 3

옵션
- --seed-users: FK(users.user_id) 충족이 필요하면 켜되(느려짐), 기본은 off.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Optional

try:
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None

try:
    import http_booking_client as http_w
except Exception:  # pragma: no cover
    http_w = None


BURST_UNIT = 1000


def _now() -> float:
    return time.monotonic()


def _percentiles(values: list[float]) -> dict:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None, "avg": None, "n": 0}
    s = sorted(values)
    n = len(s)
    return {
        "p50": round(s[n // 2], 4),
        "p95": round(s[int(n * 0.95)], 4),
        "p99": round(s[int(n * 0.99)], 4),
        "max": round(s[-1], 4),
        "avg": round(sum(s) / n, 4),
        "n": n,
    }


def _seat_for_i(i: int, *, seat_rows: int, seat_cols: int, wrap: bool) -> str:
    rows = max(1, int(seat_rows))
    cols = max(1, int(seat_cols))
    cap = rows * cols
    if not wrap and i >= cap:
        return ""
    j = i % cap
    return f"{(j // cols) + 1}-{(j % cols) + 1}"


@dataclass(frozen=True)
class EnterResult:
    ok: bool
    http: int
    user_id: int
    queue_ref: str
    latency: float


@dataclass(frozen=True)
class CommitResult:
    ok: bool
    http: int
    user_id: int
    booking_ref: str
    code: str
    latency: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Concert load v5.5 (enter burst -> admit -> commit hold -> SQS)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-n", "--count", type=int, required=True, help=f"천 단위. 총 요청 = n×{BURST_UNIT}")
    p.add_argument("--show-id", type=int, required=True)
    p.add_argument("--concert-id", type=int, default=0, help="옵션(hold 관측용 read-api 호출 시 사용)")

    # seat map (DB 조회를 피해서 '초반 폭탄'을 방해하지 않음)
    p.add_argument("--seat-rows", type=int, default=500)
    p.add_argument("--seat-cols", type=int, default=100)
    p.add_argument("--seat-wrap", action=argparse.BooleanOptionalAction, default=False)

    # enter burst
    p.add_argument("--enter-duration-sec", type=float, default=3.0)
    p.add_argument("--enter-concurrency", type=int, default=4000)
    p.add_argument("--enter-timeout-sec", type=float, default=8.0)

    # waiting-room control (기본 OFF: 테스트 왜곡 방지)
    p.add_argument("--wr-control", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--wr-mode", choices=["AUTO", "MANUAL"], default="MANUAL")
    p.add_argument("--wr-enabled", type=lambda s: str(s).lower() in ("1", "true", "yes", "y"), default=True)
    p.add_argument("--wr-admit-rate", type=int, default=300000, help="MANUAL일 때 초당 admit 인원(게이트 속도)")
    p.add_argument("--wr-message", default="loadtest v5.5")
    p.add_argument("--reset-wr", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--reset-concert-redis", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--restore-wr-auto", action=argparse.BooleanOptionalAction, default=False)

    # admit + commit (hold)
    p.add_argument("--admit-timeout-sec", type=float, default=600.0, help="ADMITTED 될 때까지 최대 대기(로스 방지 핵심)")
    p.add_argument("--status-interval-ms", type=int, default=100, help="status 폴링 시작 간격(ms)")
    p.add_argument("--status-interval-max-ms", type=int, default=2000, help="status 폴링 최대 간격(ms, backoff 상한)")
    p.add_argument("--commit-concurrency", type=int, default=800)
    p.add_argument("--commit-timeout-sec", type=float, default=20.0)
    p.add_argument("--commit-retries", type=int, default=3)
    p.add_argument("--commit-retry-backoff-ms", type=int, default=80)

    # user ids
    p.add_argument("--user-base", type=int, default=1)
    p.add_argument("--seed-users", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--db-name", default=None, help="seed-users=true일 때만 사용")

    # endpoints
    p.add_argument("--write-api-base", default=None, metavar="URL")
    p.add_argument(
        "--read-api-base",
        default=os.getenv("READ_API_BASE_URL", "").strip() or "http://read-api.ticketing.svc.cluster.local:5000",
        metavar="URL",
    )
    p.add_argument("--observe-holds-sec", type=float, default=0.0)

    # output
    p.add_argument("--progress-every", type=int, default=1000)
    return p.parse_args()


async def _req_json(
    session: "aiohttp.ClientSession",
    method: str,
    url: str,
    body: dict | None,
    *,
    timeout_sec: float,
) -> tuple[int, dict]:
    try:
        async with session.request(
            method.upper(),
            url,
            json=body,
            timeout=aiohttp.ClientTimeout(total=max(1.0, float(timeout_sec))),
        ) as resp:
            code = int(resp.status)
            try:
                j = await resp.json(content_type=None)
            except Exception:
                txt = await resp.text()
                j = {"_parse_error": True, "_raw": txt}
            return code, j if isinstance(j, dict) else {"_non_dict": True, "value": j}
    except asyncio.TimeoutError:
        return 0, {"_timeout": True}
    except Exception as e:
        return 0, {"_error": repr(e)}


async def main_async() -> int:
    if aiohttp is None:
        raise SystemExit("필요: pip install aiohttp")
    if http_w is None:
        raise SystemExit("http_booking_client 모듈을 찾을 수 없습니다.")

    args = parse_args()
    total_n = int(args.count) * BURST_UNIT
    if total_n <= 0:
        raise SystemExit("-n/--count 는 1 이상이어야 합니다.")

    show_id = int(args.show_id)
    write_base = http_w.resolve_write_api_base(args.write_api_base)
    write_base = write_base.rstrip("/")

    def _wurl(path: str) -> str:
        return f"{write_base}{path}"

    # (옵션) 유저 시드: 기본 off (초반 폭탄 연출을 깨기 쉬움)
    if bool(args.seed_users):
        # 기존 v4의 pymysql seeding 함수를 재사용(있으면)하려면 import 비용이 커져서,
        # 여기서는 seed-users를 "실습용 옵션"으로만 둔다.
        from scripts.sqs_load_real_concert5 import _ensure_loadtest_users_fast  # type: ignore
        from scripts.sqs_load_real_concert5 import _resolve_db_name  # type: ignore

        dbn = _resolve_db_name(args.db_name)
        _ensure_loadtest_users_fast(dbn, user_base=int(args.user_base), user_count=int(total_n), name_prefix="sqs-load-concert55-")

    # WR/Redis 제어는 테스트를 왜곡할 수 있어 기본 OFF.
    # 필요할 때만 --wr-control / --reset-*로 명시적으로 켠다.
    if bool(args.reset_wr):
        try:
            http_w.concert_waiting_room_reset(write_base, show_id, timeout=10.0)
        except Exception:
            pass
    if bool(args.reset_concert_redis):
        try:
            http_w.concert_redis_reset(write_base, show_id, timeout=10.0)
        except Exception:
            pass
    if bool(args.wr_control):
        try:
            http_w.concert_waiting_room_control(
                write_base,
                show_id,
                mode=str(args.wr_mode),
                enabled=bool(args.wr_enabled),
                admit_rate_per_sec=int(args.wr_admit_rate) if str(args.wr_mode).upper() == "MANUAL" else None,
                message=str(args.wr_message),
                timeout=10.0,
            )
        except Exception:
            pass

    # ──────────────────────────────────────────────
    # 1) ENTER BURST: 몇 초 안에 n만명 "대기열 진입"
    # ──────────────────────────────────────────────
    enter_ok: list[EnterResult] = []
    enter_http = Counter()
    enter_lat: list[float] = []
    enter_fail_sample: deque[tuple[int, str]] = deque(maxlen=10)

    enter_conc = max(1, int(args.enter_concurrency))
    enter_dur = max(0.001, float(args.enter_duration_sec))
    enter_timeout = max(1.0, float(args.enter_timeout_sec))

    async def _enter_one(i: int, *, session: "aiohttp.ClientSession", t0: float) -> None:
        # duration 동안 균등 발사 (폭탄이지만, 완전 동시 생성으로 이벤트루프를 죽이지 않게)
        scheduled = t0 + (enter_dur * (i / max(1, total_n)))
        now = _now()
        if scheduled > now:
            await asyncio.sleep(scheduled - now)
        uid = int(args.user_base) + i
        t1 = _now()
        code, j = await _req_json(
            session,
            "POST",
            _wurl(f"/api/write/concerts/{show_id}/waiting-room/enter"),
            {"user_id": uid},
            timeout_sec=enter_timeout,
        )
        lat = _now() - t1
        enter_http[int(code)] += 1
        enter_lat.append(lat)
        qref = str((j or {}).get("queue_ref") or "")
        if code == 200 and qref:
            enter_ok.append(EnterResult(ok=True, http=code, user_id=uid, queue_ref=qref, latency=lat))
        else:
            msg = str((j or {}).get("message") or (j or {}).get("detail") or (j or {}).get("_raw") or "")
            enter_fail_sample.append((int(code), msg[:120]))

    # ──────────────────────────────────────────────
    # 2) ADMIT + COMMIT: permit을 길게 기다려 '로스'를 줄이고, commit은 재시도로 QUEUED를 높임
    # ──────────────────────────────────────────────
    admit_timeout = max(1.0, float(args.admit_timeout_sec))
    status_iv0 = max(10, int(args.status_interval_ms)) / 1000.0
    status_iv_max = max(status_iv0, max(10, int(args.status_interval_max_ms)) / 1000.0)
    commit_conc = max(1, int(args.commit_concurrency))
    commit_timeout = max(1.0, float(args.commit_timeout_sec))
    commit_retries = max(0, int(args.commit_retries))
    backoff_ms = max(0, int(args.commit_retry_backoff_ms))

    commit_http = Counter()
    commit_code = Counter()
    commit_lat: list[float] = []
    queued_booking_refs: list[str] = []
    commit_fail_sample: deque[tuple[int, str, str]] = deque(maxlen=20)
    status_http = Counter()
    status_err = Counter()
    admit_fail_code = Counter()

    async def _admit_then_commit(er: EnterResult, *, session: "aiohttp.ClientSession") -> Optional[CommitResult]:
        import urllib.parse

        ref_enc = urllib.parse.quote(str(er.queue_ref).strip(), safe="")
        deadline = _now() + admit_timeout
        permit = ""
        # permit poll: 길게(로스 방지), 적응형 backoff로 불필요한 폴링 부하를 줄여 전체 처리량을 올린다.
        # - 처음엔 촘촘히(빠른 admit 캐치)
        # - 시간이 지나면 간격을 늘려 WR/Redis를 때리는 QPS를 제한
        poll_iv = float(status_iv0)
        last_status_http = 0
        last_status_err = ""
        while _now() < deadline:
            code1, st = await _req_json(
                session,
                "GET",
                _wurl(f"/api/write/concerts/waiting-room/status/{ref_enc}"),
                None,
                timeout_sec=min(10.0, commit_timeout),
            )
            last_status_http = int(code1)
            status_http[int(code1)] += 1
            if int(code1) == 0 and isinstance(st, dict) and st.get("_timeout"):
                last_status_err = "timeout"
                status_err["timeout"] += 1
            elif int(code1) == 0 and isinstance(st, dict) and st.get("_error"):
                last_status_err = "error"
                status_err["error"] += 1
            elif int(code1) >= 400:
                last_status_err = f"http_{int(code1)}"
                status_err[f"http_{int(code1)}"] += 1
            if (
                int(code1) == 200
                and isinstance(st, dict)
                and st.get("status") == "ADMITTED"
                and st.get("permit_token")
            ):
                permit = str(st.get("permit_token") or "")
                break
            # backoff (cap)
            await asyncio.sleep(poll_iv)
            poll_iv = min(status_iv_max, poll_iv * (1.15 + (0.05 * random.random())))

        if not permit:
            # admit 실패(대기열/폴링에서 못 받음)
            admit_fail_code["ADMIT_TIMEOUT"] += 1
            if last_status_err:
                admit_fail_code[f"last_status:{last_status_err}"] += 1
            if last_status_http:
                admit_fail_code[f"last_status_http:{last_status_http}"] += 1
            return CommitResult(
                ok=False,
                http=0,
                user_id=er.user_id,
                booking_ref="",
                code="ADMIT_TIMEOUT",
                latency=0.0,
            )

        seat_key = _seat_for_i(er.user_id - int(args.user_base), seat_rows=int(args.seat_rows), seat_cols=int(args.seat_cols), wrap=bool(args.seat_wrap))
        if not seat_key:
            admit_fail_code["NO_SEAT"] += 1
            return CommitResult(ok=False, http=0, user_id=er.user_id, booking_ref="", code="NO_SEAT", latency=0.0)

        body = {"user_id": er.user_id, "show_id": show_id, "seats": [seat_key], "permit_token": permit, "queue_ref": er.queue_ref}

        last_code = 0
        last_j: dict = {}
        last_api_code = ""
        lat = 0.0
        for attempt in range(commit_retries + 1):
            t2 = _now()
            last_code, last_j = await _req_json(session, "POST", _wurl("/api/write/concerts/booking/commit"), body, timeout_sec=commit_timeout)
            lat = _now() - t2
            last_api_code = str((last_j or {}).get("code") or "")
            ok = (int(last_code) == 200 and bool((last_j or {}).get("ok")) and last_api_code == "QUEUED")
            if ok:
                bref = str((last_j or {}).get("booking_ref") or "")
                return CommitResult(ok=True, http=int(last_code), user_id=er.user_id, booking_ref=bref, code="QUEUED", latency=lat)

            retryable = False
            if int(last_code) == 0 and (last_j or {}).get("_timeout"):
                retryable = True
            elif int(last_code) in (500, 503):
                retryable = True
            elif int(last_code) == 429 and last_api_code == "WAITING_ROOM_REQUIRED":
                # permit 만료/유실 케이스: status로 재발급 시도를 유도 (서버가 지원)
                retryable = True

            if not retryable or attempt >= commit_retries:
                break

            # 지수 backoff + jitter
            if backoff_ms > 0:
                base = (backoff_ms * (2**attempt)) / 1000.0
                await asyncio.sleep(min(2.0, base + random.random() * 0.05))

        return CommitResult(
            ok=False,
            http=int(last_code),
            user_id=er.user_id,
            booking_ref=str((last_j or {}).get("booking_ref") or ""),
            code=last_api_code or "COMMIT_FAIL",
            latency=lat,
        )

    # (옵션) hold 관측: read-api가 보여주는 주황이 즉시 늘어나는지 확인
    observe_sec = max(0.0, float(args.observe_holds_sec or 0.0))
    read_base = str(args.read_api_base or "").strip().rstrip("/")
    concert_id = int(args.concert_id or 0)
    observe_enabled = bool(observe_sec > 0 and concert_id > 0 and show_id > 0 and read_base)

    async def _observe_loop(session: "aiohttp.ClientSession") -> None:
        if not observe_enabled:
            return
        url = f"{read_base}/api/read/concert/{concert_id}/booking-holds?show_id={show_id}"
        end = _now() + observe_sec
        last = ""
        while _now() < end:
            code, j = await _req_json(session, "GET", url, None, timeout_sec=5.0)
            if int(code) == 200 and isinstance(j, dict) and j.get("ok"):
                hc = j.get("hold_count")
                rev = j.get("hold_rev")
                s = f"rev={rev} hold_count={hc}"
                if s != last:
                    print(f"[observe] {s}", file=sys.stderr)
                    last = s
            await asyncio.sleep(0.5)

    # execute
    t0_all = _now()
    connector = aiohttp.TCPConnector(limit=max(enter_conc, commit_conc), limit_per_host=max(enter_conc, commit_conc))
    async with aiohttp.ClientSession(connector=connector) as session:
        t0_enter = _now()
        # enter: queue-based producer/consumer
        q: asyncio.Queue[Optional[int]] = asyncio.Queue(maxsize=enter_conc * 2)

        async def _prod_enter() -> None:
            for i in range(total_n):
                await q.put(i)
            for _ in range(enter_conc):
                await q.put(None)

        async def _work_enter() -> None:
            while True:
                i = await q.get()
                if i is None:
                    return
                await _enter_one(int(i), session=session, t0=t0_enter)

        await asyncio.gather(_prod_enter(), *[_work_enter() for _ in range(enter_conc)])
        enter_elapsed = max(0.001, _now() - t0_enter)

        print(
            f"[enter] ok={len(enter_ok):,}/{total_n:,}  "
            f"elapsed={enter_elapsed:.2f}s  rps={len(enter_ok)/enter_elapsed:.0f}  "
            f"http={enter_http.most_common(5)}  "
            f"lat={_percentiles(enter_lat)}",
            file=sys.stderr,
        )

        # admit+commit: 제한된 동시성으로 진행 (로스율 개선 포인트)
        observe_task = asyncio.create_task(_observe_loop(session)) if observe_enabled else None
        sem = asyncio.Semaphore(commit_conc)
        done = 0

        async def _one_commit(er: EnterResult) -> None:
            nonlocal done
            async with sem:
                r = await _admit_then_commit(er, session=session)
            if r is None:
                return
            commit_http[int(r.http)] += 1
            if r.code:
                commit_code[str(r.code)] += 1
            if r.latency:
                commit_lat.append(float(r.latency))
            if r.ok and r.booking_ref:
                queued_booking_refs.append(r.booking_ref)
            else:
                commit_fail_sample.append((int(r.http), str(r.code), str(r.booking_ref)[:36]))
            done += 1
            if done % int(args.progress_every) == 0 or done == len(enter_ok):
                elapsed = max(0.001, _now() - t0_all)
                queued = len(queued_booking_refs)
                loss = max(0, len(enter_ok) - queued)
                loss_pct = (loss / max(1, len(enter_ok))) * 100
                print(
                    f"progress: committed={done:,}/{len(enter_ok):,}  queued={queued:,}  "
                    f"loss={loss:,}({loss_pct:.1f}%)  "
                    f"commit_code_top={commit_code.most_common(3)}  "
                    f"commit_http_top={commit_http.most_common(3)}  "
                    f"elapsed={elapsed:.1f}s",
                    file=sys.stderr,
                )

        await asyncio.gather(*[_one_commit(er) for er in enter_ok])

        if observe_task is not None:
            observe_task.cancel()

    # restore WR auto (best-effort). 기본 OFF: 입력기가 서버 상태를 바꾸지 않게.
    if bool(args.restore_wr_auto) and bool(args.wr_control):
        try:
            http_w.concert_waiting_room_control(
                write_base,
                show_id,
                mode="AUTO",
                enabled=True,
                admit_rate_per_sec=None,
                message="",
                timeout=10.0,
            )
        except Exception:
            pass

    # summary
    total_elapsed = _now() - t0_all
    queued = len(queued_booking_refs)
    base = len(enter_ok)
    loss = max(0, base - queued)
    loss_pct = round((loss / max(1, base)) * 100, 2)

    summary = {
        "version": "concert55",
        "show_id": show_id,
        "total_enter_requested": total_n,
        "enter_ok": base,
        "admit_attempted": base,
        "commit_queued_ok": queued,
        "loss_count": loss,
        "loss_rate_pct_vs_enter_ok": loss_pct,
        "enter_http_status": dict(enter_http),
        "status_http_status": dict(status_http),
        "status_errors": dict(status_err),
        "admit_fail_breakdown": dict(admit_fail_code),
        "commit_http_status": dict(commit_http),
        "commit_api_code": dict(commit_code),
        "enter_latency_sec": _percentiles(enter_lat),
        "commit_latency_sec": _percentiles(commit_lat),
        "enter_fail_sample": list(enter_fail_sample),
        "commit_fail_sample": list(commit_fail_sample),
        "queued_booking_refs_sample": queued_booking_refs[:5],
        "elapsed_sec": round(total_elapsed, 3),
        "notes": {
            "loss_definition": "loss = enter_ok 대비 QUEUED 실패. (테스트는 admit_timeout을 충분히 두어 '클라이언트 포기' 로스를 줄인다)",
            "ui_hold": "commit 성공 시 write-api가 Redis hold set/hold_rev + remain 선차감으로 즉시 반영",
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(
        f"\n{'='*60}\n"
        f"  ENTER OK:   {base:,}/{total_n:,}\n"
        f"  SQS QUEUED:  {queued:,}\n"
        f"  LOSS(vs enter_ok): {loss:,} ({loss_pct}%)\n"
        f"  status_http_top: {status_http.most_common(5)}\n"
        f"  commit_http_top:  {commit_http.most_common(5)}\n"
        f"  commit_code_top:  {commit_code.most_common(5)}\n"
        f"  enter P99:  {summary['enter_latency_sec']['p99']}s\n"
        f"  commit P99: {summary['commit_latency_sec']['p99']}s\n"
        f"{'='*60}\n",
        file=sys.stderr,
    )
    return 0


def main() -> None:
    try:
        code = asyncio.run(main_async())
    except KeyboardInterrupt:
        raise SystemExit(130)
    raise SystemExit(code)


if __name__ == "__main__":
    main()

