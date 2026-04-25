#!/usr/bin/env python3
"""
콘서트 예매 "버스트" 부하 생성기 v2.

목표:
- 3만건 같은 회차(show_id)에 대해서도 짧은 시간에 대량 write 요청을 몰아 넣어
  KEDA(worker-svc) scale-out + 완료시간 단축 효과가 선명하게 보이게 한다.

특징:
- --via-was 사용 시 Write API POST를 스레드풀로 병렬 전송(=버스트).
- 표준 라이브러리(urllib) HTTP 클라이언트(`scripts/http_booking_client.py`)를 그대로 사용.
- 좌석 선택/회차 선택/유저 준비는 기존 sqs_load_real_concert.py 흐름을 유지.

실행 예(클러스터 내부 tools-once):
  cd /work/ticketing-db/terraform &&
  WRITE_API_BASE_URL="http://write-api.ticketing.svc.cluster.local:5001" \
  python3 ../scripts/sqs_load_real_concert2.py -n 30000 --spread-users 30000 --via-was
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Set, Tuple

try:
    import pymysql
    from pymysql.cursors import DictCursor
    from pymysql.err import OperationalError
except ImportError:
    print("필요: pip install pymysql", file=sys.stderr)
    sys.exit(1)

import http_booking_client as http_w

DEFAULT_CONCERT_TITLE = "2026 봄 페스티벌 LIVE - 5만석"


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
    host = _resolve_db_writer_host()
    port = int(os.getenv("DB_PORT", "3306"))
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASSWORD", "")
    try:
        return pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=db_name,
            charset="utf8mb4",
            cursorclass=DictCursor,
        )
    except OperationalError as e:
        raise


def _ensure_user(cur, uid: int, *, dry_run: bool, name_prefix: str) -> None:
    cur.execute("SELECT 1 FROM users WHERE user_id = %s LIMIT 1", (uid,))
    if cur.fetchone():
        return
    if dry_run:
        return
    name = f"{name_prefix}{uid}"
    for pfx in (1555, 1556, 1557):
        phone = f"+{pfx}{uid:010d}"[:20]
        cur.execute(
            "INSERT IGNORE INTO users (user_id, phone, password_hash, name) VALUES (%s, %s, %s, %s)",
            (uid, phone, "loadtest", name),
        )
        cur.execute("SELECT 1 FROM users WHERE user_id = %s LIMIT 1", (uid,))
        if cur.fetchone():
            return


def _booked_seat_pairs(cur, show_id: int) -> Set[Tuple[int, int]]:
    cur.execute(
        """
        SELECT seat_row_no, seat_col_no
        FROM concert_booking_seats
        WHERE show_id = %s AND status = 'ACTIVE'
        """,
        (show_id,),
    )
    return {(int(r["seat_row_no"]), int(r["seat_col_no"])) for r in cur.fetchall()}


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
          AND cs.remain_count > 0
          AND cs.show_date >= NOW()
        ORDER BY cs.show_date ASC, cs.remain_count DESC
        LIMIT 1
        """,
        (concert_title,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit("조건에 맞는 회차가 없습니다. --show-id로 지정하세요.")
    return row


def _collect_free_seat_keys(
    seat_rows: int,
    seat_cols: int,
    booked: Set[Tuple[int, int]],
    limit: int,
) -> list[str]:
    out: list[str] = []
    for r in range(1, seat_rows + 1):
        for c in range(1, seat_cols + 1):
            if (r, c) in booked:
                continue
            out.append(f"{r}-{c}")
            if len(out) >= limit:
                return out
    return out


def parse_args():
    p = argparse.ArgumentParser(description="콘서트 예매 버스트 부하 v2 (Write API 병렬 전송)")
    p.add_argument("-n", "--count", type=int, required=True, help="예매 건수(좌석 1개/건)")
    p.add_argument("--show-id", type=int, default=None, help="미지정 시 공연 제목으로 회차 자동 선택")
    p.add_argument("--concert-title", default=DEFAULT_CONCERT_TITLE)
    p.add_argument("--user-id", type=int, default=None)
    p.add_argument(
        "--spread-users",
        type=int,
        default=1,
        metavar="K",
        help="K>1이면 건마다 user_id를 base..base+K-1 순환. base는 --user-id 또는 1",
    )
    p.add_argument("--db-name", default=None, metavar="NAME")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--via-was", action="store_true", help="Write API(유저 경로)로만 전송")
    p.add_argument("--write-api-base", default=None, metavar="URL")
    p.add_argument(
        "--http-workers",
        type=int,
        default=int(os.getenv("BURST_HTTP_WORKERS", "100") or 100),
        help="Write API 병렬 전송 워커 수(기본 100). 너무 크면 API/DB가 먼저 포화될 수 있음.",
    )
    p.add_argument(
        "--http-timeout",
        type=float,
        default=float(os.getenv("BURST_HTTP_TIMEOUT", "30") or 30),
        help="Write API 요청 타임아웃(초, 기본 30)",
    )
    p.add_argument(
        "--progress-every",
        type=int,
        default=500,
        help="진행 로그 출력 간격(성공+실패 기준, 기본 500)",
    )
    p.add_argument(
        "--wait",
        dest="wait",
        action="store_true",
        help="접수(QUEUED) 후 worker 백그라운드 커밋 완료(OK)까지 폴링해 총 소요를 측정",
    )
    p.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="접수(QUEUED)까지만 측정(기존 동작)",
    )
    p.set_defaults(wait=True)
    p.add_argument(
        "--poll-timeout",
        type=float,
        default=float(os.getenv("BURST_POLL_TIMEOUT", "600") or 600),
        help="--wait 사용 시 booking status 폴링 타임아웃(초, 기본 600)",
    )
    p.add_argument(
        "--poll-workers",
        type=int,
        default=int(os.getenv("BURST_POLL_WORKERS", "50") or 50),
        help="--wait 사용 시 폴링 병렬 워커 수(기본 50)",
    )
    return p.parse_args()


def main():
    args = parse_args()
    if args.count < 1:
        raise SystemExit("--count 는 1 이상")
    spread = max(1, int(args.spread_users))
    if spread < 1:
        raise SystemExit("--spread-users 는 1 이상")
    if not args.via_was:
        raise SystemExit("이 v2 스크립트는 --via-was(Write API 버스트) 전용입니다.")

    t0 = time.monotonic()
    dbn = _resolve_db_name(args.db_name)
    conn = _db_connect(dbn)
    conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            np = "sqs-load-concert-"
            uid_base = int(args.user_id) if args.user_id is not None else 1
            # user 준비
            if spread > 1:
                for u in range(uid_base, uid_base + spread):
                    _ensure_user(cur, u, dry_run=args.dry_run, name_prefix=np)
            else:
                _ensure_user(cur, uid_base, dry_run=args.dry_run, name_prefix=np)

            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()

            show = _pick_show(cur, args.show_id, args.concert_title)
            sid = int(show["show_id"])
            rows = int(show["seat_rows"])
            cols = int(show["seat_cols"])
            remain = int(show["remain_count"])

            cap = min(int(args.count), remain)
            booked = _booked_seat_pairs(cur, sid)
            seats = _collect_free_seat_keys(rows, cols, booked, cap)
            if len(seats) < cap:
                print(
                    f"경고: 요청 {args.count}건 중 빈 좌석 {len(seats)}개만 확보(remain={remain}).",
                    file=sys.stderr,
                )
            if not seats:
                raise SystemExit("예약 가능한 좌석이 없습니다.")

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "회차_id": sid,
                        "공연제목": show["concert_title"],
                        "요청_건수": args.count,
                        "실제_건수": len(seats),
                        "spread_users": spread,
                        "http_workers": int(args.http_workers),
                        "샘플_좌석": seats[:5],
                        "소요_초": round(time.monotonic() - t0, 3),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        write_base = http_w.resolve_write_api_base(args.write_api_base)
        per_msg_uids = [uid_base + (i % spread) for i in range(len(seats))] if spread > 1 else [uid_base] * len(seats)

        total = len(seats)
        workers = max(1, int(args.http_workers))
        timeout = max(1.0, float(args.http_timeout))
        progress_every = max(1, int(args.progress_every))

        ok = 0
        fail = 0
        accepted_refs: list[str] = []
        accepted_ok = 0
        accepted_fail = 0
        t_send0 = time.monotonic()

        def _one(i: int):
            uid = int(per_msg_uids[i])
            sk = seats[i]
            # 1) Waiting Room 입장권(permit) 획득
            code0, j0 = http_w.concert_waiting_room_enter(write_base, uid, sid, timeout=min(10.0, timeout))
            qref = str((j0 or {}).get("queue_ref") or "")
            if code0 != 200 or not qref:
                return False, code0, sk, ""

            permit = ""
            # 2) 내 순번이 올 때까지 폴링(입장 허가)
            deadline = time.monotonic() + max(1.0, float(args.poll_timeout))
            while time.monotonic() < deadline:
                _, st = http_w.concert_waiting_room_status(write_base, qref, timeout=min(10.0, timeout))
                if isinstance(st, dict) and st.get("status") == "ADMITTED" and st.get("permit_token"):
                    permit = str(st.get("permit_token"))
                    break
                time.sleep(0.4)

            if not permit:
                return False, 408, sk, ""

            # 3) permit 포함하여 커밋(이제서야 예매 요청이 접수됨)
            code, j = http_w.request_json(
                f"{write_base}/api/write/concerts/booking/commit",
                "POST",
                {"user_id": uid, "show_id": sid, "seats": [sk], "permit_token": permit},
                timeout=timeout,
            )
            ref = str((j or {}).get("booking_ref") or "")
            success = (code == 200 and (j or {}).get("ok") and bool(ref) and str((j or {}).get("code") or "") == "QUEUED")
            return success, code, sk, ref

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, i) for i in range(total)]
            done = 0
            for fut in as_completed(futures):
                success, code, seat_key, ref = fut.result()
                done += 1
                if success:
                    ok += 1
                    if ref:
                        accepted_refs.append(ref)
                else:
                    fail += 1
                    if fail <= 5:
                        print(f"FAIL http={code} seat={seat_key}", file=sys.stderr)
                if done % progress_every == 0 or done == total:
                    elapsed = max(0.001, time.monotonic() - t_send0)
                    qps = done / elapsed
                    print(f"progress: {done}/{total} ok={ok} fail={fail} qps={qps:.1f}", file=sys.stderr)

        send_sec = time.monotonic() - t_send0

        # --wait: 백그라운드 처리 완료(OK)까지 폴링해 "대기열"이 실제로 보이게 한다.
        poll_sec = 0.0
        if args.wait and accepted_refs:
            poll_workers = max(1, int(args.poll_workers))
            poll_timeout = max(1.0, float(args.poll_timeout))
            t_poll0 = time.monotonic()

            def _poll_one(ref: str):
                result = http_w.poll_booking_status(
                    write_base,
                    ref,
                    kind="concert",
                    timeout_sec=poll_timeout,
                    interval_sec=0.4,
                )
                ok2 = bool(isinstance(result, dict) and result.get("ok") is True and str(result.get("code") or "") == "OK")
                return ok2, result

            inflight = len(accepted_refs)
            print(f"queued: {inflight}개 — worker 백그라운드 처리 대기/진행 중...", file=sys.stderr)
            with ThreadPoolExecutor(max_workers=poll_workers) as pool2:
                futs2 = [pool2.submit(_poll_one, r) for r in accepted_refs]
                done2 = 0
                for fut in as_completed(futs2):
                    ok2, result = fut.result()
                    done2 += 1
                    if ok2:
                        accepted_ok += 1
                    else:
                        accepted_fail += 1
                        if accepted_fail <= 5:
                            code = (result or {}).get("code") if isinstance(result, dict) else None
                            status = (result or {}).get("status") if isinstance(result, dict) else None
                            print(f"POLL_FAIL code={code} status={status}", file=sys.stderr)
                    if done2 % progress_every == 0 or done2 == inflight:
                        elapsed2 = max(0.001, time.monotonic() - t_poll0)
                        qps2 = done2 / elapsed2
                        print(
                            f"poll: {done2}/{inflight} ok={accepted_ok} fail={accepted_fail} qps={qps2:.1f}",
                            file=sys.stderr,
                        )
            poll_sec = time.monotonic() - t_poll0

        summary = {
            "회차_id": sid,
            "공연_id": int(show["concert_id"]),
            "공연제목": show["concert_title"],
            "회차_일시": str(show["show_date"]),
            "정원": int(show["total_count"]),
            "잔여_요청시점": remain,
            "좌석_격자": f"{rows}x{cols}",
            "요청_건수": int(args.count),
            "실제_건수": total,
            "spread_users": spread,
            "http_workers": workers,
            "http_timeout_sec": timeout,
            "HTTP_접수_성공": ok,
            "HTTP_접수_실패": fail,
            "전송_소요_초": round(send_sec, 3),
            "전송_QPS": round((total / send_sec) if send_sec > 0 else 0.0, 2),
            "wait": bool(args.wait),
            "POLL_완료_OK": int(accepted_ok),
            "POLL_완료_실패": int(accepted_fail),
            "폴링_소요_초": round(poll_sec, 3),
            "총_소요_초": round(time.monotonic() - t0, 3),
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        if fail or (args.wait and accepted_fail):
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

