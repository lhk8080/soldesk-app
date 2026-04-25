#!/usr/bin/env python3
"""
콘서트 실제 좌석 기준 예매 부하 — DB에서 빈 (행,열)을 잡아 SQS 또는 Write API로 전송.

기본 실험 대상: db-schema/Insert.sql 의 5만석 시드
  - 공연 제목: 「2026 봄 페스티벌 LIVE - 5만석」

  • 기본: SQS FIFO (워커 본문과 동일 JSON, MessageGroupId = show_id-user_id).
  • --via-was: POST /api/write/concerts/booking/commit 만.
  • --also-via-was: 좌석 반씩 SQS + Write API.

  pip install boto3 pymysql

DB: DB_WRITER_HOST(미설정 시 terraform output rds_writer_endpoint), DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
SQS: (sqs_건수 > 0 일 때) SQS_QUEUE_URL / --queue-url / terraform output
Write API: (write_api_건수 > 0) WRITE_API_BASE_URL / --write-api-base

주의: 실제 concert_booking / concert_booking_seats / concert_payment 가 쌓인다.
회차 선택: RDS NOW() 기준 cs.show_date >= NOW() (자동·--show-id 공통).

예:
  python3 ../scripts/sqs_load_real_concert.py -n 100 --dry-run
  python3 ../scripts/sqs_load_real_concert.py -n 50 --also-via-was
  python3 ../scripts/sqs_load_real_concert.py -n 20 --via-was --http-poll
  # user_id 1..50 순환 → FIFO 그룹(show-user) 분산
  python3 ../scripts/sqs_load_real_concert.py -n 10000 --spread-users 50 --also-via-was
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Set, Tuple

try:
    import boto3
    import pymysql
    from botocore.exceptions import ClientError
    from pymysql.cursors import DictCursor
    from pymysql.err import OperationalError
except ImportError:
    print("필요: pip install boto3 pymysql", file=sys.stderr)
    sys.exit(1)

import http_booking_client as http_w

# db-schema/Insert.sql — 5만석 콘서트 시드
DEFAULT_CONCERT_TITLE = "2026 봄 페스티벌 LIVE - 5만석"


def _seat_shard_id(seat_key: str, shards: int) -> int:
    """seat_key="r-c" → show 내부 샤드 id (FIFO 그룹 분산용)."""
    try:
        r_s, c_s = seat_key.split("-", 1)
        r = int(r_s)
        c = int(c_s)
    except Exception:
        return 0
    n = max(1, min(1024, int(shards)))
    return ((r * 1000003) ^ c) % n


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


def _queue_url_from_terraform() -> str:
    return _terraform_output_raw("sqs_queue_url")


def _resolve_db_writer_host() -> str:
    h = (os.getenv("DB_WRITER_HOST") or "").strip()
    if h:
        return h
    ep = _terraform_output_raw("rds_writer_endpoint")
    if ep:
        print(
            "DB_WRITER_HOST 미설정 → terraform output rds_writer_endpoint 사용",
            file=sys.stderr,
        )
        return ep
    raise SystemExit(
        "DB_WRITER_HOST 가 없고 terraform output rds_writer_endpoint 도 실패했습니다.\n"
        "  terraform/ 에서 init·apply 후 실행하거나, 로컬 MySQL이면 export DB_WRITER_HOST=127.0.0.1"
    )


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
        if e.args and e.args[0] == 1049:
            print(
                f"MySQL DB '{db_name}' 없음 (host={host}). "
                f"--db-name 으로 실제 DB 이름 지정하거나, 해당 서버에 create.sql 로 스키마 적용.",
                file=sys.stderr,
            )
        if e.args and e.args[0] == 2003:
            print(
                "RDS 연결 타임아웃: 엔드포인트는 맞지만 이 머신에서 VPC 안 RDS(3306)로 라우팅이 안 됩니다.\n"
                "  같은 VPC의 EC2/EKS 파드에서 실행하거나, 베스천·SSM 포트포워딩·VPN 등으로 DB 경로를 연 뒤 다시 시도하세요.\n"
                "  (보안그룹 인바운드에 해당 소스에서 3306 허용 여부도 확인)",
                file=sys.stderr,
            )
        raise


def _ensure_user(cur, uid: int, *, dry_run: bool, name_prefix: str) -> None:
    """있으면 끝. 없으면 INSERT IGNORE 최대 3회(전화번호만 다름). 재시도 루프·에러 종료 없음."""
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


def _resolve_booking_user_id(cur, prefer: int, *, dry_run: bool, name_prefix: str) -> int:
    """예매 메시지에 넣을 user_id: prefer → 1 → 최소 id → (실행 시) 자동 증가 한 행."""
    cur.execute("SELECT user_id FROM users WHERE user_id = %s LIMIT 1", (prefer,))
    r = cur.fetchone()
    if r:
        return int(r["user_id"])
    cur.execute("SELECT user_id FROM users WHERE user_id = 1 LIMIT 1")
    r = cur.fetchone()
    if r:
        return int(r["user_id"])
    cur.execute("SELECT user_id FROM users ORDER BY user_id ASC LIMIT 1")
    r = cur.fetchone()
    if r:
        return int(r["user_id"])
    if dry_run:
        return prefer
    phone = ("+1789" + uuid.uuid4().hex[:12])[:20]
    cur.execute(
        "INSERT INTO users (phone, password_hash, name) VALUES (%s, %s, %s)",
        (phone, "loadtest", f"{name_prefix}fallback"),
    )
    return int(cur.lastrowid)


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
            raise SystemExit(
                f"show_id={show_id} 없음 또는 상영일시가 이미 지났습니다(RDS NOW() 기준)."
            )
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
        raise SystemExit(
            f"조건에 맞는 회차 없음 (공연 제목={concert_title!r}, OPEN, remain>0, show_date>=NOW()). "
            "Insert.sql 시드 확인 또는 --show-id 로 미래 회차 지정."
        )
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
    p = argparse.ArgumentParser(description="콘서트 실좌석 SQS 예매 부하 (기본: 5만석 시드 회차)")
    p.add_argument("-n", "--count", type=int, required=True, help="예매 건수(메시지 수), 좌석 1개/건")
    p.add_argument("--show-id", type=int, default=None, help="미지정 시 공연 제목으로 회차 자동 선택")
    p.add_argument(
        "--concert-title",
        default=DEFAULT_CONCERT_TITLE,
        help=f"자동 회차 선택 시 공연 제목 (기본: {DEFAULT_CONCERT_TITLE!r})",
    )
    p.add_argument("--user-id", type=int, default=None)
    p.add_argument(
        "--spread-users",
        type=int,
        default=1,
        metavar="K",
        help="K>1이면 건마다 user_id를 base..base+K-1 순환(SQS FIFO 그룹 분산). base는 --user-id 또는 1",
    )
    p.add_argument(
        "--db-name",
        default=None,
        metavar="NAME",
        help="스키마(DB) 이름. 미지정이면 환경변수 DB_NAME, 둘 다 없으면 ticketing",
    )
    p.add_argument("--queue-url", default=os.getenv("SQS_QUEUE_URL", "").strip())
    p.add_argument(
        "--fifo-shards",
        type=int,
        default=int(os.getenv("CONCERT_FIFO_SHARDS", "64") or 64),
        help="SQS FIFO MessageGroupId를 show_id 단일이 아니라 show_id+shard로 분산(기본 64)",
    )
    p.add_argument(
        "--region",
        default=os.getenv("AWS_DEFAULT_REGION", os.getenv("AWS_REGION", "ap-northeast-2")),
    )
    p.add_argument("--dry-run", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--via-was",
        action="store_true",
        help="SQS 생략, Write API(유저와 동일 POST)로만 전송",
    )
    g.add_argument(
        "--also-via-was",
        action="store_true",
        help="좌석을 반으로 나눠 SQS + Write API 둘 다 사용",
    )
    p.add_argument(
        "--write-api-base",
        default=None,
        metavar="URL",
        help="Write API 베이스 URL (미지정 시 WRITE_API_BASE_URL)",
    )
    p.add_argument(
        "--http-poll",
        action="store_true",
        help="Write API 접수 후 GET .../concerts/booking/status 로 폴링",
    )
    p.add_argument(
        "--poll-sec",
        type=float,
        default=120.0,
        help="--http-poll 시 건당 최대 대기(초)",
    )
    return p.parse_args()


def main():
    args = parse_args()
    if args.count < 1:
        raise SystemExit("--count 는 1 이상")
    if int(args.spread_users) < 1:
        raise SystemExit("--spread-users 는 1 이상")

    queue_url = args.queue_url or _queue_url_from_terraform()

    t0 = time.monotonic()
    dbn = _resolve_db_name(args.db_name)
    conn = _db_connect(dbn)
    conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            np = "sqs-load-concert-"
            spread = max(1, int(args.spread_users))
            uid_base = 1
            if spread > 1:
                uid_base = int(args.user_id) if args.user_id is not None else 1
                for u in range(uid_base, uid_base + spread):
                    _ensure_user(cur, u, dry_run=args.dry_run, name_prefix=np)
                user_id = uid_base
            else:
                if args.user_id is not None:
                    _ensure_user(cur, args.user_id, dry_run=args.dry_run, name_prefix=np)
                    prefer = args.user_id
                else:
                    _ensure_user(cur, 1, dry_run=args.dry_run, name_prefix=np)
                    _ensure_user(cur, 2, dry_run=args.dry_run, name_prefix=np)
                    prefer = 1
                user_id = _resolve_booking_user_id(
                    cur, prefer, dry_run=args.dry_run, name_prefix=np
                )
            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()

            show = _pick_show(cur, args.show_id, args.concert_title)
            sid = int(show["show_id"])
            rows = int(show["seat_rows"])
            cols = int(show["seat_cols"])
            remain = int(show["remain_count"])

            cap = min(args.count, remain)
            booked = _booked_seat_pairs(cur, sid)
            seats = _collect_free_seat_keys(rows, cols, booked, cap)

            if len(seats) < cap:
                print(
                    f"경고: 요청 {args.count}건 중 빈 좌석 {len(seats)}개만 확보 (remain={remain}).",
                    file=sys.stderr,
                )
            if not seats:
                raise SystemExit("예약 가능한 좌석이 없습니다.")

            n_alloc = len(seats)
            if args.via_was:
                sqs_n, http_n = 0, n_alloc
            elif args.also_via_was:
                sqs_n = n_alloc // 2
                http_n = n_alloc - sqs_n
            else:
                sqs_n, http_n = n_alloc, 0

            sqs_seats = seats[:sqs_n]
            http_seats = seats[sqs_n:]

            per_msg_uids = (
                [uid_base + (i % spread) for i in range(n_alloc)]
                if spread > 1
                else [user_id] * n_alloc
            )

            bodies: list[dict] = []
            for i, key in enumerate(sqs_seats):
                bodies.append(
                    {
                        "booking_type": "concert",
                        "booking_ref": str(uuid.uuid4()),
                        "user_id": per_msg_uids[i],
                        "show_id": sid,
                        "seats": [key],
                    }
                )

        summary: dict = {
            "회차_id": sid,
            "공연_id": int(show["concert_id"]),
            "공연제목": show["concert_title"],
            "회차_일시": str(show["show_date"]),
            "정원": int(show["total_count"]),
            "잔여_요청시점": remain,
            "좌석_격자": f"{rows}x{cols}",
            "유저_id": user_id,
            "요청_건수": args.count,
            "실제_건수": n_alloc,
            "경로": {"sqs_건수": sqs_n, "write_api_건수": http_n},
            "메시지_수": len(bodies) + len(http_seats),
            "fifo_shards": int(args.fifo_shards),
            "샘플_좌석": seats[:5],
        }
        if spread > 1:
            summary["유저_spread"] = {
                "base": uid_base,
                "n": spread,
                "범위": f"{uid_base}..{uid_base + spread - 1} 순환",
            }

        if args.dry_run:
            summary["소요_초"] = round(time.monotonic() - t0, 3)
            summary["테스트후_잔여좌석"] = remain
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            if bodies:
                print("--- dry-run SQS 본문 샘플(최대 3) ---")
                for b in bodies[:3]:
                    print(json.dumps(b, ensure_ascii=False))
            if http_seats:
                print("--- dry-run Write API POST 샘플(최대 3) ---")
                for hi, sk in enumerate(http_seats[:3]):
                    print(
                        json.dumps(
                            {
                                "user_id": per_msg_uids[sqs_n + hi],
                                "show_id": sid,
                                "seats": [sk],
                            },
                            ensure_ascii=False,
                        )
                    )
            return

        if sqs_n > 0 and not queue_url:
            raise SystemExit("SQS 경로 사용 시 큐 URL 필요 (SQS_QUEUE_URL / --queue-url / terraform output)")
        write_base = ""
        if http_n > 0:
            write_base = http_w.resolve_write_api_base(args.write_api_base)

        sent = 0
        errors = 0
        if sqs_n > 0:
            sqs = boto3.client("sqs", region_name=args.region)
            for batch_start in range(0, len(bodies), 10):
                chunk = bodies[batch_start : batch_start + 10]
                entries = []
                for j, body in enumerate(chunk):
                    raw = json.dumps(body, sort_keys=True)
                    seat_key = (body.get("seats") or [""])[0]
                    shard = _seat_shard_id(str(seat_key), int(args.fifo_shards))
                    entries.append(
                        {
                            "Id": str(j),
                            "MessageBody": raw,
                            "MessageGroupId": f"{sid}-sh{shard}",
                        }
                    )
                try:
                    resp = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
                except ClientError as e:
                    print(f"send_message_batch 실패: {e}", file=sys.stderr)
                    sys.exit(1)
                for f in resp.get("Failed", []) or []:
                    errors += 1
                    print(f"Failed: {f}", file=sys.stderr)
                sent += len(resp.get("Successful", []) or [])

        http_ok = 0
        http_fail = 0
        http_refs: list[tuple[str, str]] = []
        if http_n > 0:
            for hi, sk in enumerate(http_seats):
                uid = per_msg_uids[sqs_n + hi]
                code, j = http_w.concert_commit(write_base, uid, sid, [sk])
                ref = str((j or {}).get("booking_ref") or "")
                if code == 200 and (j or {}).get("ok") and ref:
                    http_ok += 1
                    http_refs.append((ref, sk))
                else:
                    http_fail += 1
                    print(f"Write API 실패 HTTP {code} seat={sk} body={j!r}", file=sys.stderr)

        polled: dict[str, dict] = {}
        if args.http_poll and http_refs:
            for ref, sk in http_refs:
                polled[f"{ref}({sk})"] = http_w.poll_booking_status(
                    write_base,
                    ref,
                    "concert",
                    timeout_sec=float(args.poll_sec),
                )
            summary["HTTP_폴링_결과"] = polled

        summary["SQS_전송_성공"] = sent
        summary["SQS_전송_실패"] = errors
        summary["HTTP_접수_성공"] = http_ok
        summary["HTTP_접수_실패"] = http_fail
        summary["전송_성공"] = sent + http_ok
        summary["전송_실패"] = errors + http_fail
        summary["소요_초"] = round(time.monotonic() - t0, 3)
        c_rem = _db_connect(dbn)
        try:
            c_rem.autocommit(True)
            with c_rem.cursor() as cur:
                cur.execute(
                    "SELECT remain_count FROM concert_shows WHERE show_id = %s",
                    (sid,),
                )
                _row = cur.fetchone()
            summary["테스트후_잔여좌석"] = int(_row["remain_count"]) if _row else None
        finally:
            c_rem.close()
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        if errors or http_fail:
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
