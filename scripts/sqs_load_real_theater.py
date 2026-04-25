#!/usr/bin/env python3
"""
극장(theater) 실제 좌석 기준 예매 부하 — DB에서 빈 좌석을 조회한 뒤 아래 중 선택해 전송한다.

  • 기본: SQS FIFO 직접 전송 (워커 본문과 동일 JSON, MessageGroupId = schedule_id-user_id).
    API 는 send_message_batch 를 쓰지만 **Entry마다 별도 SQS 메시지 1건**이다.
  • --via-was: Write API POST /api/write/theaters/booking/commit 만 (유저 경로와 동일).
  • --also-via-was: 좌석 목록을 반으로 나눠 앞쪽은 SQS, 뒤쪽은 Write API (한 번에 두 경로 검증).

  pip install boto3 pymysql

DB: DB_WRITER_HOST(미설정 시 terraform output rds_writer_endpoint), DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
SQS: SQS_QUEUE_URL / --queue-url / terraform output (sqs_건수 > 0 일 때)
Write API: WRITE_API_BASE_URL 또는 --write-api-base (write_api_건수 > 0 일 때)
  클러스터 예: http://write-api.ticketing.svc.cluster.local:5001

주의: 실제 booking / payment / booking_seats 가 쌓인다.

  시드 홀 정원은 보통 30 전후 → -n 은 정원 이하 권장.
  회차 선택: show_date >= '지금'(기본 Asia/Seoul wall-clock, naive DATETIME 와 동일 축).
    시드 상영시각은 지역 시각으로 넣고 RDS NOW() 는 UTC 인 경우가 많아, NOW() 직접 비교 시
    이미 지난 회차가 잘못 선택될 수 있음. TZ 는 환경변수 SCHEDULE_CUTOFF_TZ 로 변경 가능.

JSON 시간 필드(실전 전송 시): sqs_load_real_concert.py 와 동일 키
  (준비·전송·큐_소진_대기·폴링·전체·소요_초, --no-wait-sqs-queue 등).

예:
  python3 ../scripts/sqs_load_real_theater.py -n 30 --dry-run
  python3 ../scripts/sqs_load_real_theater.py -n 15 --also-via-was
  python3 ../scripts/sqs_load_real_theater.py -n 10 --via-was --http-poll
  python3 ../scripts/sqs_load_real_theater.py -n 30 --spread-users 10 --also-via-was
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Optional
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

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
from sqs_load_common import wait_sqs_queue_idle


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


def _schedule_cutoff_naive() -> datetime:
    """DB show_date(시드: 로컬 wall DATETIME)와 같은 축으로 '지금' 이후만 고른다."""
    tz_name = (os.getenv("SCHEDULE_CUTOFF_TZ") or "Asia/Seoul").strip()
    try:
        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
    except Exception:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def _pick_schedule(cur, schedule_id: Optional[int]):
    cutoff = _schedule_cutoff_naive()
    if schedule_id is not None:
        cur.execute(
            """
            SELECT s.schedule_id, s.hall_id,
                   GREATEST(0, s.total_count - IFNULL(bs.cnt, 0)) AS remain_count,
                   CASE
                     WHEN GREATEST(0, s.total_count - IFNULL(bs.cnt, 0)) <= 0 THEN 'CLOSED'
                     WHEN UPPER(COALESCE(s.status, '')) = 'CLOSED' THEN 'CLOSED'
                     ELSE 'OPEN'
                   END AS status,
                   s.movie_id, s.show_date, s.total_count, m.title AS movie_title
            FROM schedules s
            INNER JOIN movies m ON m.movie_id = s.movie_id
            LEFT JOIN (
                SELECT schedule_id, COUNT(*) AS cnt FROM booking_seats
                WHERE UPPER(COALESCE(status, '')) = 'ACTIVE'
                GROUP BY schedule_id
            ) bs ON bs.schedule_id = s.schedule_id
            WHERE s.schedule_id = %s
              AND s.show_date >= %s
            """,
            (schedule_id, cutoff),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit(
                f"schedule_id={schedule_id} 없음 또는 상영일시가 이미 지났습니다 "
                f"(기준 시각={cutoff.isoformat(sep=' ')}, TZ={os.getenv('SCHEDULE_CUTOFF_TZ') or 'Asia/Seoul'})."
            )
        return row
    cur.execute(
        """
        SELECT s.schedule_id, s.hall_id,
               GREATEST(0, s.total_count - IFNULL(bs.cnt, 0)) AS remain_count,
               CASE
                 WHEN GREATEST(0, s.total_count - IFNULL(bs.cnt, 0)) <= 0 THEN 'CLOSED'
                 WHEN UPPER(COALESCE(s.status, '')) = 'CLOSED' THEN 'CLOSED'
                 ELSE 'OPEN'
               END AS status,
               s.movie_id, s.show_date, s.total_count, m.title AS movie_title
        FROM schedules s
        INNER JOIN movies m ON m.movie_id = s.movie_id
        LEFT JOIN (
            SELECT schedule_id, COUNT(*) AS cnt FROM booking_seats
            WHERE UPPER(COALESCE(status, '')) = 'ACTIVE'
            GROUP BY schedule_id
        ) bs ON bs.schedule_id = s.schedule_id
        WHERE UPPER(COALESCE(s.status, '')) = 'OPEN'
          AND GREATEST(0, s.total_count - IFNULL(bs.cnt, 0)) > 0
          AND s.show_date >= %s
        ORDER BY s.show_date ASC, GREATEST(0, s.total_count - IFNULL(bs.cnt, 0)) DESC
        LIMIT 1
        """,
        (cutoff,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit(
            f"OPEN·remain>0·상영일시>=지금 인 schedules 가 없습니다. "
            f"(기준 시각={cutoff.isoformat(sep=' ')}, TZ={os.getenv('SCHEDULE_CUTOFF_TZ') or 'Asia/Seoul'})"
        )
    return row


def _available_seat_keys(cur, schedule_id: int, hall_id: int, limit: int) -> list[str]:
    cur.execute(
        """
        SELECT hs.seat_row_no, hs.seat_col_no
        FROM hall_seats hs
        WHERE hs.hall_id = %s
          AND NOT EXISTS (
            SELECT 1 FROM booking_seats bs
            WHERE bs.schedule_id = %s
              AND bs.seat_id = hs.seat_id
              AND bs.status = 'ACTIVE'
          )
        ORDER BY hs.seat_row_no, hs.seat_col_no
        LIMIT %s
        """,
        (hall_id, schedule_id, limit),
    )
    out = []
    for r in cur.fetchall():
        out.append(f'{int(r["seat_row_no"])}-{int(r["seat_col_no"])}')
    return out


def parse_args():
    p = argparse.ArgumentParser(description="실제 극장 좌석으로 SQS 예매 메시지 전송")
    p.add_argument("-n", "--count", type=int, required=True, help="예매 건수(메시지 수), 좌석 1개씩/건")
    p.add_argument("--schedule-id", type=int, default=None, help="미지정 시 remain 많은 OPEN 회차 자동")
    p.add_argument("--user-id", type=int, default=None, help="미지정 시 users 최소 user_id")
    p.add_argument(
        "--spread-users",
        type=int,
        default=1,
        metavar="K",
        help="K>1이면 건마다 user_id를 base..base+K-1 순환(FIFO 그룹 분산). base는 --user-id 또는 1",
    )
    p.add_argument(
        "--db-name",
        default=None,
        metavar="NAME",
        help="스키마(DB) 이름. 미지정이면 환경변수 DB_NAME, 둘 다 없으면 ticketing",
    )
    p.add_argument("--queue-url", default=os.getenv("SQS_QUEUE_URL", "").strip())
    p.add_argument(
        "--region",
        default=os.getenv("AWS_DEFAULT_REGION", os.getenv("AWS_REGION", "ap-northeast-2")),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="전송 없이 조회 결과와 앞 몇 건 본문·HTTP 샘플만 출력",
    )
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
        help="Write API 접수 후 GET /api/write/booking/status 로 결과 폴링(--poll-sec)",
    )
    p.add_argument(
        "--poll-sec",
        type=float,
        default=120.0,
        help="--http-poll 시 건당 최대 대기(초), 기본 120",
    )
    p.add_argument(
        "--no-wait-sqs-queue",
        action="store_true",
        help="SQS 전송 후 큐(가시+인플라이트+지연)가 비워질 때까지 대기하지 않음",
    )
    p.add_argument(
        "--wait-queue-timeout-sec",
        type=float,
        default=900.0,
        metavar="SEC",
        help="큐 소진 대기 최대(초), 기본 900",
    )
    p.add_argument(
        "--wait-queue-poll-sec",
        type=float,
        default=2.0,
        metavar="SEC",
        help="큐 깊이 폴링 간격(초), 기본 2",
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
    conn = _db_connect(_resolve_db_name(args.db_name))
    conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            np = "sqs-load-theater-"
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

            sch = _pick_schedule(cur, args.schedule_id)
            sid = int(sch["schedule_id"])
            hall_id = int(sch["hall_id"])
            remain = int(sch["remain_count"])

            cap = min(args.count, remain)
            seats = _available_seat_keys(cur, sid, hall_id, cap)
            if len(seats) < cap:
                print(
                    f"경고: 요청 {args.count}건 중 빈 좌석 {len(seats)}개만 사용 (remain={remain}).",
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
                        "booking_type": "theater",
                        "booking_ref": str(uuid.uuid4()),
                        "user_id": per_msg_uids[i],
                        "schedule_id": sid,
                        "seats": [key],
                    }
                )

        summary: dict = {
            "회차_id": sid,
            "영화_id": int(sch["movie_id"]),
            "영화제목": sch["movie_title"],
            "상영일시": str(sch["show_date"]),
            "정원": int(sch["total_count"]),
            "잔여_요청시점": remain,
            "홀_id": hall_id,
            "유저_id": user_id,
            "요청_건수": args.count,
            "실제_건수": n_alloc,
            "경로": {"sqs_건수": sqs_n, "write_api_건수": http_n},
            "메시지_수": len(bodies) + len(http_seats),
            "샘플_좌석": seats[:5],
        }
        if spread > 1:
            summary["유저_spread"] = {
                "base": uid_base,
                "n": spread,
                "범위": f"{uid_base}..{uid_base + spread - 1} 순환",
            }

        if args.dry_run:
            _t1 = time.monotonic()
            summary["준비_소요_초"] = round(_t1 - t0, 3)
            summary["전송_소요_초"] = 0.0
            summary["큐_소진_대기_초"] = 0.0
            summary["큐_대기_타임아웃"] = False
            summary["폴링_소요_초"] = 0.0
            summary["전체_소요_초"] = round(_t1 - t0, 3)
            summary["소요_초"] = summary["전체_소요_초"]
            summary["테스트후_잔여좌석"] = remain - args.count
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            if bodies:
                print("--- dry-run SQS 본문 샘플(최대 3) ---")
                for b in bodies[:3]:
                    print(json.dumps(b, ensure_ascii=False))
            if http_seats:
                print("--- dry-run Write API POST 본문 샘플(최대 3) ---")
                for hi, sk in enumerate(http_seats[:3]):
                    print(
                        json.dumps(
                            {
                                "user_id": per_msg_uids[sqs_n + hi],
                                "schedule_id": sid,
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
        sqs = None
        t_tx_start = time.monotonic()
        if sqs_n > 0:
            sqs = boto3.client("sqs", region_name=args.region)
            for batch_start in range(0, len(bodies), 10):
                chunk = bodies[batch_start : batch_start + 10]
                entries = []
                for j, body in enumerate(chunk):
                    raw = json.dumps(body, sort_keys=True)
                    entries.append(
                        {
                            "Id": str(j),
                            "MessageBody": raw,
                            "MessageGroupId": f"{sid}-{body['user_id']}",
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
                code, j = http_w.theater_commit(write_base, uid, sid, [sk])
                ref = str((j or {}).get("booking_ref") or "")
                if code == 200 and (j or {}).get("ok") and ref:
                    http_ok += 1
                    http_refs.append((ref, sk))
                else:
                    http_fail += 1
                    print(f"Write API 실패 HTTP {code} seat={sk} body={j!r}", file=sys.stderr)

        polled: dict[str, dict] = {}
        poll_sec = 0.0
        if args.http_poll and http_refs:
            t_poll0 = time.monotonic()
            for ref, sk in http_refs:
                polled[f"{ref}({sk})"] = http_w.poll_booking_status(
                    write_base,
                    ref,
                    "theater",
                    timeout_sec=float(args.poll_sec),
                )
            poll_sec = round(time.monotonic() - t_poll0, 3)
            summary["HTTP_폴링_결과"] = polled

        t_tx_done = time.monotonic()
        prep_sec = round(t_tx_start - t0, 3)
        tx_sec = round(t_tx_done - t_tx_start, 3)

        summary["SQS_전송_성공"] = sent
        summary["SQS_전송_실패"] = errors
        summary["HTTP_접수_성공"] = http_ok
        summary["HTTP_접수_실패"] = http_fail
        summary["전송_성공"] = sent + http_ok
        summary["전송_실패"] = errors + http_fail

        if sqs_n > 0 and not args.no_wait_sqs_queue and sqs is not None:
            summary.update(
                wait_sqs_queue_idle(
                    sqs,
                    queue_url,
                    timeout_sec=float(args.wait_queue_timeout_sec),
                    poll_interval_sec=float(args.wait_queue_poll_sec),
                )
            )
        else:
            summary["큐_소진_대기_초"] = 0.0
            summary["큐_대기_타임아웃"] = False
            summary["큐_종료_가시"] = None
            summary["큐_종료_인플라이트"] = None
            summary["큐_종료_지연"] = None

        t_final = time.monotonic()
        summary["준비_소요_초"] = prep_sec
        summary["전송_소요_초"] = tx_sec
        summary["폴링_소요_초"] = poll_sec
        summary["전체_소요_초"] = round(t_final - t0, 3)
        summary["소요_초"] = summary["전체_소요_초"]
        summary["테스트후_잔여좌석"] = remain - args.count
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        if errors or http_fail:
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
