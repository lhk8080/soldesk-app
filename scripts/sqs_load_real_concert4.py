#!/usr/bin/env python3
"""
콘서트 "10초에 3만명 쓰기 시도" 연출용 부하 생성기 v4.

요지
- DB에 직접 INSERT 하지 않고, 실제 유저처럼 Write API(HTTP/WAS)를 통해 요청을 보낸다.
- 목표는 "총 N건을 10초(또는 duration) 안에 발사"하는 것(=버스트 트래픽 연출).

핵심 전략
- Waiting Room을 MANUAL + 높은 admit_rate_per_sec 로 설정해 ADMITTED가 빨리 나도록 만든다.
- enter → (짧게) status 확인 → permit_token 얻으면 commit 를 수행한다.
- 좌석은 DB에서 조회하지 않고, (row,col) 그리드를 기준으로 i번째 요청마다 고유 seat_key를 만든다.
  (좌석 충돌/품절을 일부러 만들고 싶으면 --seat-wrap 을 켜거나 grid를 작게 잡으면 된다.)

실행 예 (클러스터 내부 tools-once 파드에서 권장)
  WRITE_API_BASE_URL="http://write-api.ticketing.svc.cluster.local:5001" \
  python3 ../scripts/sqs_load_real_concert4.py --show-id 8 -n 30000 --duration-sec 10 \
    --user-base 1 --seat-rows 500 --seat-cols 100 --http-concurrency 2000

준비물
  pip install aiohttp pymysql

주의
- users FK 때문에, user_id 범위에 대한 users 시드가 필요하다(본 스크립트가 자동 seed).
- 실제로 DB에 쓰기(booking) 트래픽이 발생하므로 운영 DB에는 사용 금지.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Optional

import http_booking_client as http_w


DEFAULT_CONCERT_TITLE = "2026 봄 페스티벌 LIVE - 5만석"
DEFAULT_BURST_UNIT = 1000  # -n COUNT 는 "단위 수", 총 요청수 = COUNT * DEFAULT_BURST_UNIT


def _terraform_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "terraform"


def _terraform_output_raw(name: str) -> str:
    tf_dir = _terraform_dir()
    if not tf_dir.is_dir():
        return ""
    try:
        proc = subprocess.run(
            ["terraform", "output", "-raw", name],
            cwd=str(tf_dir),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _resolve_db_writer_host() -> str:
    h = (os.getenv("DB_WRITER_HOST") or "").strip()
    if h:
        return h
    ep = _terraform_output_raw("rds_writer_endpoint")
    if ep:
        return ep
    raise SystemExit("DB_WRITER_HOST 미설정이며 terraform output도 실패했습니다.")


def _resolve_db_name(cli_db: Optional[str]) -> str:
    if cli_db is not None and str(cli_db).strip():
        return str(cli_db).strip()
    env = (os.getenv("DB_NAME") or "").strip()
    if env:
        return env
    return "ticketing"


def _db_connect(db_name: str):
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError:
        print("필요: pip install pymysql", file=sys.stderr)
        raise

    host = _resolve_db_writer_host()
    port = int(os.getenv("DB_PORT", "3306"))
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASSWORD", "")
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db_name,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )


def _pick_show(cur, show_id: Optional[int], concert_title: str):
    if show_id is not None:
        cur.execute(
            """
            SELECT cs.show_id, cs.concert_id, cs.show_date, cs.seat_rows, cs.seat_cols,
                   cs.total_count, cs.remain_count, cs.status, c.title AS concert_title
            FROM concert_shows cs
            INNER JOIN concerts c ON c.concert_id = cs.concert_id
            WHERE cs.show_id = %s
              AND cs.show_date >= NOW()
            """,
            (show_id,),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"show_id={show_id} 없음 또는 show_date가 지났습니다.")
        return row
    cur.execute(
        """
        SELECT cs.show_id, cs.concert_id, cs.show_date, cs.seat_rows, cs.seat_cols,
               cs.total_count, cs.remain_count, cs.status, c.title AS concert_title
        FROM concert_shows cs
        INNER JOIN concerts c ON c.concert_id = cs.concert_id
        WHERE c.title = %s
          AND UPPER(COALESCE(cs.status, '')) = 'OPEN'
          AND cs.show_date >= NOW()
        ORDER BY cs.show_date ASC
        LIMIT 1
        """,
        (concert_title,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit("조건에 맞는 회차가 없습니다. --show-id로 지정하세요.")
    return row


def _ensure_loadtest_users_fast(
    db_name: str, *, user_base: int, user_count: int, name_prefix: str = "sqs-load-concert4-"
) -> None:
    """
    FK(users.user_id) 때문에 필요한 유저를 미리 보장한다.
    v3의 1명당 SELECT/INSERT 루프 대신, INSERT IGNORE executemany로 빠르게 시드한다.
    """
    n = int(user_count)
    if n <= 0:
        return
    ub = int(user_base)
    uid_hi = ub + n - 1

    conn = _db_connect(db_name)
    try:
        with conn.cursor() as cur:
            rows = []
            for uid in range(ub, uid_hi + 1):
                name = f"{name_prefix}{uid}"
                phone = f"+1555{uid:010d}"[:20]
                rows.append((uid, phone, "loadtest", name))
            # (user_id, phone)에 유니크 제약이 걸려 있을 수 있어 phone 충돌을 피하기 위해 uid를 섞는다.
            cur.executemany(
                "INSERT IGNORE INTO users (user_id, phone, password_hash, name) VALUES (%s, %s, %s, %s)",
                rows,
            )
        print(f"[info] users ensured user_id={ub}..{uid_hi} (n={n})", file=sys.stderr)
    finally:
        conn.close()


def _seat_for_i(i: int, *, seat_rows: int, seat_cols: int, wrap: bool) -> str:
    rows = max(1, int(seat_rows))
    cols = max(1, int(seat_cols))
    cap = rows * cols
    if not wrap and i >= cap:
        return ""
    j = i % cap
    r = (j // cols) + 1
    c = (j % cols) + 1
    return f"{r}-{c}"


def parse_args():
    p = argparse.ArgumentParser(description="콘서트 10초 버스트(HTTP/WAS) 부하 v4")
    p.add_argument(
        "-n",
        "--count",
        type=int,
        required=True,
        help=f"요청 '단위' 수. 실제 총 요청수는 (count * {DEFAULT_BURST_UNIT}). 예: -n 40 => 40000",
    )
    p.add_argument("--duration-sec", type=float, default=10.0, help="버스트 발사 기간(초, 기본 10)")
    p.add_argument("--show-id", type=int, default=None)
    p.add_argument("--concert-title", default=DEFAULT_CONCERT_TITLE)
    p.add_argument("--write-api-base", default=None, metavar="URL")
    p.add_argument("--db-name", default=None, metavar="NAME")
    p.add_argument("--user-base", type=int, default=1, help="가상 유저 id 시작값(기본 1)")
    p.add_argument("--seed-users", action=argparse.BooleanOptionalAction, default=True, help="users 시드 여부(기본 true)")
    p.add_argument("--seat-rows", type=int, default=500, help="하드코딩 좌석 row 수(기본 500)")
    p.add_argument("--seat-cols", type=int, default=100, help="하드코딩 좌석 col 수(기본 100) -> 기본 5만석")
    p.add_argument(
        "--seat-wrap",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="요청 수가 rows*cols를 넘어가면 좌석을 wrap(중복)할지(기본 false=초과는 스킵)",
    )
    p.add_argument(
        "--http-concurrency",
        type=int,
        default=int(os.getenv("BURST_HTTP_CONCURRENCY", "2000") or 2000),
        help="동시 in-flight HTTP 수(기본 2000). 너무 크면 클라이언트/서버가 먼저 터질 수 있음.",
    )
    p.add_argument(
        "--http-timeout",
        type=float,
        default=float(os.getenv("BURST_HTTP_TIMEOUT", "20") or 20),
        help="개별 HTTP 타임아웃(초, 기본 20)",
    )
    p.add_argument(
        "--admit-rate",
        type=int,
        default=int(os.getenv("WR_ADMIT_RATE", "300000") or 300000),
        help="waiting-room MANUAL admit_rate_per_sec (기본 300000)",
    )
    p.add_argument(
        "--reset-wr",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="시작 시 waiting-room 카운터 reset(기본 true)",
    )
    p.add_argument(
        "--reset-concert-redis",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="시작 시 콘서트 좌석/잔여 Redis 키 reset(기본 true)",
    )
    p.add_argument(
        "--restore-wr-auto",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="종료 시 waiting-room control을 AUTO로 복원(기본 true)",
    )
    p.add_argument(
        "--status-poll-max-sec",
        type=float,
        default=2.0,
        help="enter 후 permit 얻기 위해 status를 폴링하는 최대 시간(초, 기본 2.0). 짧게 잡아 '시도' 연출에 집중.",
    )
    p.add_argument(
        "--admit-timeout-sec",
        type=float,
        default=0.0,
        help=(
            "permit(ADMITTED) 대기 최대 시간(초). 0이면 --status-poll-max-sec 값을 사용. "
            "콘서트3처럼 '결국 커밋까지 가서 SQS 적체/오토스케일'을 보려면 30~600 권장."
        ),
    )
    p.add_argument("--status-interval-ms", type=int, default=50, help="status 폴링 간격(ms, 기본 50)")
    p.add_argument("--progress-every", type=int, default=1000)
    p.add_argument(
        "--plan",
        default="all-at-once",
        choices=["uniform", "all-at-once"],
        help="all-at-once=즉시 최대 동시 발사(기본, 과부하/스파이크), uniform=duration 동안 균등 분산 발사",
    )
    p.add_argument(
        "--read-api-base",
        default=os.getenv("READ_API_BASE_URL", "").strip() or "http://read-api.ticketing.svc.cluster.local:5000",
        metavar="URL",
        help="read-api 베이스 URL(주황 hold 좌석 관측용). 기본: env READ_API_BASE_URL 또는 클러스터 서비스 5000",
    )
    p.add_argument(
        "--observe-holds-sec",
        type=float,
        default=0.0,
        help="0보다 크면 실행 중/직후 read-api booking-holds를 폴링해 hold_rev/hold_count를 출력",
    )
    p.add_argument(
        "--observe-interval-sec",
        type=float,
        default=0.5,
        help="--observe-holds-sec 사용 시 폴링 간격(초, 기본 0.5)",
    )
    p.add_argument(
        "--observe-holds-sample",
        type=int,
        default=10,
        help="hold_seats에서 출력할 샘플 개수(기본 10, 서버 응답 크기 영향 최소화)",
    )
    return p.parse_args()


async def main_async() -> int:
    args = parse_args()
    n = int(args.count) * int(DEFAULT_BURST_UNIT)
    if n < 1:
        raise SystemExit("--count 는 1 이상")
    duration = max(0.001, float(args.duration_sec))
    conc = max(1, int(args.http_concurrency))
    timeout = max(1.0, float(args.http_timeout))

    write_base = http_w.resolve_write_api_base(args.write_api_base)

    # show_id 선택(좌석 grid 기본값을 show에서 가져오고 싶으면 여기서 가능)
    dbn = _resolve_db_name(args.db_name)
    show_id = None
    concert_id = None
    concert_title = None
    remain_at_start = None
    try:
        conn = _db_connect(dbn)
        try:
            with conn.cursor() as cur:
                show = _pick_show(cur, args.show_id, args.concert_title)
                show_id = int(show["show_id"])
                concert_id = int(show.get("concert_id") or 0)
                concert_title = show.get("concert_title")
                remain_at_start = int(show.get("remain_count") or 0)
        finally:
            conn.close()
    except Exception as e:
        raise SystemExit(f"show 선택을 위한 DB 조회 실패: {e!r}")

    if show_id is None:
        raise SystemExit("show_id 해석 실패")

    # users seed
    if bool(args.seed_users):
        _ensure_loadtest_users_fast(dbn, user_base=int(args.user_base), user_count=n)

    # WR/Redis reset + WR control(초고속 admit)
    if bool(args.reset_wr):
        try:
            http_w.concert_waiting_room_reset(write_base, show_id, timeout=min(10.0, timeout))
        except Exception:
            pass
    if bool(args.reset_concert_redis):
        try:
            http_w.concert_redis_reset(write_base, show_id, timeout=min(10.0, timeout))
        except Exception:
            pass
    try:
        http_w.concert_waiting_room_control(
            write_base,
            show_id,
            mode="MANUAL",
            enabled=True,
            admit_rate_per_sec=int(args.admit_rate),
            message="loadtest burst v4",
            timeout=min(10.0, timeout),
        )
    except Exception as e:
        print(f"[warn] waiting-room control 실패(계속 진행): {e!r}", file=sys.stderr)

    # aiohttp 준비
    try:
        import aiohttp
    except ImportError:
        print("필요: pip install aiohttp", file=sys.stderr)
        raise

    sem = asyncio.Semaphore(conc)
    progress_every = max(1, int(args.progress_every))
    poll_max_cli = max(0.0, float(args.status_poll_max_sec))
    admit_timeout_cli = max(0.0, float(getattr(args, "admit_timeout_sec", 0.0) or 0.0))
    poll_max = admit_timeout_cli if admit_timeout_cli > 0 else poll_max_cli
    poll_iv = max(1, int(args.status_interval_ms)) / 1000.0

    counters = {
        "enter_ok": 0,
        "enter_fail": 0,
        "admitted_ok": 0,
        "admitted_fail": 0,
        "commit_queued_ok": 0,
        "commit_fail": 0,
        "skipped_no_seat": 0,
        "http_429_wr_required": 0,
        "http_other_fail": 0,
    }
    enter_http = Counter()
    commit_http = Counter()
    commit_api_code = Counter()
    recent_commit_fail = deque(maxlen=10)  # (http_status, api_code, message)
    lock = asyncio.Lock()
    accepted_refs: list[str] = []

    def _url(path: str) -> str:
        return f"{write_base}{path}"

    async def _req_json(session: "aiohttp.ClientSession", method: str, url: str, body: dict | None):
        try:
            async with session.request(
                method.upper(),
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                code = int(resp.status)
                try:
                    j = await resp.json(content_type=None)
                except Exception:
                    txt = await resp.text()
                    j = {"_parse_error": True, "_raw": txt}
                return code, j
        except asyncio.TimeoutError:
            return 0, {"_timeout": True}
        except Exception as e:
            return 0, {"_error": repr(e)}

    # (옵션) 주황(hold) 생성/감소 관측: read-api booking-holds 폴링
    observe_sec = max(0.0, float(getattr(args, "observe_holds_sec", 0.0) or 0.0))
    observe_iv = max(0.1, float(getattr(args, "observe_interval_sec", 0.5) or 0.5))
    observe_sample_n = max(1, int(getattr(args, "observe_holds_sample", 10) or 10))
    read_base = str(getattr(args, "read_api_base", "") or "").strip().rstrip("/")

    observe_enabled = (
        observe_sec > 0.0
        and concert_id is not None
        and int(concert_id or 0) > 0
        and int(show_id or 0) > 0
        and bool(read_base)
    )

    def _observe_url() -> str:
        # read-api: /api/read/concert/{concert_id}/booking-holds?show_id={show_id}
        # (기존 코드 패턴 유지: URL quote 안전문자 포함)
        return f"{read_base}/api/read/concert/{int(concert_id)}/booking-holds?show_id={int(show_id)}"

    async def _one(i: int, *, t0: float):
        # 발사 스케줄링(10초 내 N건)
        if str(args.plan) == "uniform":
            scheduled = t0 + (duration * (i / max(1, n)))
            now = time.monotonic()
            if scheduled > now:
                await asyncio.sleep(scheduled - now)

        uid = int(args.user_base) + int(i)
        seat_key = _seat_for_i(
            int(i), seat_rows=int(args.seat_rows), seat_cols=int(args.seat_cols), wrap=bool(args.seat_wrap)
        )
        if not seat_key:
            async with lock:
                counters["skipped_no_seat"] += 1
            return

        async with sem:
            # 1) enter
            code0, j0 = await _req_json(
                session,
                "POST",
                _url(f"/api/write/concerts/{int(show_id)}/waiting-room/enter"),
                {"user_id": uid},
            )
            qref = str((j0 or {}).get("queue_ref") or "")
            if code0 != 200 or not qref:
                async with lock:
                    counters["enter_fail"] += 1
                    enter_http[int(code0)] += 1
                return
            async with lock:
                counters["enter_ok"] += 1
                enter_http[int(code0)] += 1

            # 2) status poll (짧게)
            permit = ""
            deadline = time.monotonic() + poll_max
            ref_enc = None
            if qref:
                import urllib.parse

                ref_enc = urllib.parse.quote(str(qref).strip(), safe="")
            while poll_max > 0 and time.monotonic() < deadline:
                code1, st = await _req_json(
                    session,
                    "GET",
                    _url(f"/api/write/concerts/waiting-room/status/{ref_enc}"),
                    None,
                )
                if isinstance(st, dict) and st.get("status") == "ADMITTED" and st.get("permit_token"):
                    permit = str(st.get("permit_token"))
                    break
                await asyncio.sleep(poll_iv)

            if not permit:
                async with lock:
                    counters["admitted_fail"] += 1
                return
            async with lock:
                counters["admitted_ok"] += 1

            # 3) commit (permit 포함)
            code2, j2 = await _req_json(
                session,
                "POST",
                _url("/api/write/concerts/booking/commit"),
                {"user_id": uid, "show_id": int(show_id), "seats": [seat_key], "permit_token": permit},
            )
            api_code = str((j2 or {}).get("code") or "")
            ok = (code2 == 200 and (j2 or {}).get("ok") and api_code == "QUEUED")
            if ok:
                bref = str((j2 or {}).get("booking_ref") or "")
                async with lock:
                    counters["commit_queued_ok"] += 1
                    commit_http[int(code2)] += 1
                    if api_code:
                        commit_api_code[api_code] += 1
                    if bref:
                        accepted_refs.append(bref)
                return

            async with lock:
                counters["commit_fail"] += 1
                commit_http[int(code2)] += 1
                if api_code:
                    commit_api_code[api_code] += 1
                if code2 == 429 and api_code == "WAITING_ROOM_REQUIRED":
                    counters["http_429_wr_required"] += 1
                else:
                    counters["http_other_fail"] += 1
                try:
                    msg = str((j2 or {}).get("message") or (j2 or {}).get("detail") or "")
                except Exception:
                    msg = ""
                recent_commit_fail.append((int(code2), api_code, msg[:120]))

    t_all0 = time.monotonic()
    t_fire0 = None
    try:
        async with aiohttp.ClientSession() as session:
            async def _observe_holds_loop() -> None:
                if not observe_enabled:
                    return
                deadline = time.monotonic() + observe_sec
                last_rev = None
                max_hold = 0
                max_sample: list[str] = []
                url = _observe_url()
                print(f"[observe] holds url={url}", file=sys.stderr)
                while time.monotonic() < deadline:
                    try:
                        code_o, j_o = await _req_json(session, "GET", url, None)
                    except Exception:
                        await asyncio.sleep(observe_iv)
                        continue
                    if int(code_o or 0) != 200 or not isinstance(j_o, dict) or not j_o.get("ok"):
                        await asyncio.sleep(observe_iv)
                        continue
                    rev = j_o.get("hold_rev")
                    hc = j_o.get("hold_count")
                    try:
                        rev_i = int(rev or 0)
                    except Exception:
                        rev_i = 0
                    try:
                        hc_i = int(hc or 0)
                    except Exception:
                        hc_i = 0
                    hs = j_o.get("hold_seats")
                    if isinstance(hs, list):
                        sample = [str(x) for x in hs[:observe_sample_n]]
                    else:
                        sample = []
                    if hc_i > max_hold:
                        max_hold = hc_i
                        max_sample = sample
                    if (last_rev is None) or (rev_i != last_rev) or (hc_i > 0):
                        last_rev = rev_i
                        print(
                            f"[observe] hold_rev={rev_i} hold_count={hc_i} sample={sample}",
                            file=sys.stderr,
                        )
                    await asyncio.sleep(observe_iv)
                print(
                    f"[observe] done max_hold_count={max_hold} max_sample={max_sample}",
                    file=sys.stderr,
                )

            observe_task = asyncio.create_task(_observe_holds_loop()) if observe_enabled else None

            t_fire0 = time.monotonic()
            tasks = [asyncio.create_task(_one(i, t0=t_fire0)) for i in range(n)]

            done = 0
            for fut in asyncio.as_completed(tasks):
                await fut
                done += 1
                if done % progress_every == 0 or done == n:
                    async with lock:
                        elapsed = max(0.001, time.monotonic() - t_fire0)
                        qps = done / elapsed
                        top_enter = enter_http.most_common(3)
                        top_commit = commit_api_code.most_common(3)
                        print(
                            f"progress: {done}/{n} qps={qps:.1f} "
                            f"enter_ok={counters['enter_ok']} enter_fail={counters['enter_fail']} "
                            f"admit_ok={counters['admitted_ok']} admit_fail={counters['admitted_fail']} "
                            f"queued_ok={counters['commit_queued_ok']} commit_fail={counters['commit_fail']} "
                            f"wr429={counters['http_429_wr_required']} "
                            f"enter_http_top={top_enter} commit_code_top={top_commit}",
                            file=sys.stderr,
                        )
    finally:
        if "observe_task" in locals() and observe_task:
            observe_task.cancel()
        if bool(args.restore_wr_auto):
            try:
                http_w.concert_waiting_room_control(
                    write_base,
                    show_id,
                    mode="AUTO",
                    enabled=True,
                    admit_rate_per_sec=None,
                    message="",
                    timeout=min(10.0, timeout),
                )
            except Exception:
                pass

    total_sec = time.monotonic() - t_all0
    fire_sec = max(0.001, time.monotonic() - (t_fire0 or t_all0))

    summary = {
        "show_id": int(show_id),
        "concert_id": int(concert_id or 0),
        "concert_title": concert_title,
        "remain_at_start": remain_at_start,
        "count": int(n),
        "count_args": {"count": int(args.count), "unit": int(DEFAULT_BURST_UNIT)},
        "duration_sec": float(duration),
        "plan": str(args.plan),
        "http_concurrency": int(conc),
        "http_timeout_sec": float(timeout),
        "seat_grid": f"{int(args.seat_rows)}x{int(args.seat_cols)}",
        "seat_wrap": bool(args.seat_wrap),
        "status_poll_max_sec": float(poll_max_cli),
        "admit_timeout_sec": float(admit_timeout_cli) if admit_timeout_cli > 0 else None,
        "permit_wait_max_sec_effective": float(poll_max),
        "status_interval_ms": int(args.status_interval_ms),
        "wr_admit_rate_per_sec": int(args.admit_rate),
        "stats": counters,
        "enter_http_status": dict(enter_http),
        "commit_http_status": dict(commit_http),
        "commit_api_code": dict(commit_api_code),
        "recent_commit_fail": list(recent_commit_fail),
        "accepted_refs": len(accepted_refs),
        "accepted_refs_sample": accepted_refs[: min(5, len(accepted_refs))],
        "fire_elapsed_sec": round(fire_sec, 3),
        "total_elapsed_sec": round(total_sec, 3),
        "fire_qps": round((n / fire_sec) if fire_sec > 0 else 0.0, 2),
        "note": "commit_queued_ok는 접수(QUEUED)이며 최종 OK/FAIL은 worker 처리 후 status에서 결정됨",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    try:
        code = asyncio.run(main_async())
    except KeyboardInterrupt:
        raise SystemExit(130)
    raise SystemExit(code)


if __name__ == "__main__":
    main()

