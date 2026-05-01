#!/usr/bin/env python3
"""
뮤지컬 회차(시드: 뮤지컬 <별이 빛나는 밤>) — RDS NOW() 이후 중 가장 가까운 show_date 회차 기준
유저 두 명이 같은 좌석을 동시에 노리는 SQS 메시지를 약 10건 병렬 전송한다.

  (DB 시드에 '왕과 사는 남자' 제목은 없음 → Insert.sql 의 뮤지컬 제목을 사용.
   공연명을 바꿔 쓰려면 --concert-title)

  pip install boto3 pymysql redis

  export DB_PASSWORD='...'
  python3 ../scripts/sqs_race_two_users_musical.py
  python3 ../scripts/sqs_race_two_users_musical.py --seat 3-5

  --seat 미지정 시 첫 빈 좌석. user_id 1·2 없으면 자동 생성, 있으면 그대로 사용.

  여러 좌석 겹침 + 결과 요약 (Redis booking:result; 호스트 미설정 시 terraform elasticache_primary_endpoint):
  python3 ../scripts/sqs_race_two_users_musical.py \\
    --user1-seats 1-2,1-3,1-4 --user2-seats 1-3,1-4,1-5

  Write API(유저 경로)만 쓰려면 --via-was (WRITE_API_BASE_URL / --write-api-base).

전송 후 DB 확인: ../scripts/sql/musical_race_verify.sql (@show_id 설정)

SQS 경로: 전송 → 큐 소진 대기(기본) → Redis 폴링. JSON 시간 키는 콘서트 부하 스크립트와 맞춤
  (추가: 폴링_소요_초). --no-wait-sqs-queue 로 큐 대기 생략 가능.
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

try:
    import boto3
    import pymysql
    from botocore.exceptions import ClientError
    from pymysql.cursors import DictCursor
    from pymysql.err import IntegrityError, OperationalError
except ImportError:
    print("필요: pip install boto3 pymysql", file=sys.stderr)
    sys.exit(1)

import http_booking_client as http_w
from sqs_load_common import wait_sqs_queue_idle

DEFAULT_MUSICAL_TITLE = "뮤지컬 <별이 빛나는 밤>"


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


def _resolve_db_name(cli_db: str | None) -> str:
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


def _nearest_musical_show(cur, concert_title: str) -> dict:
    cur.execute(
        """
        SELECT cs.show_id, cs.concert_id, cs.show_date, cs.seat_rows, cs.seat_cols,
               GREATEST(0, cs.total_count - IFNULL(cb.cnt, 0)) AS remain_count,
               CASE
                 WHEN GREATEST(0, cs.total_count - IFNULL(cb.cnt, 0)) <= 0 THEN 'CLOSED'
                 WHEN UPPER(COALESCE(cs.status, '')) = 'CLOSED' THEN 'CLOSED'
                 ELSE 'OPEN'
               END AS status,
               c.title
        FROM concert_shows cs
        INNER JOIN concerts c ON c.concert_id = cs.concert_id
        LEFT JOIN (
            SELECT show_id, COUNT(*) AS cnt FROM concert_booking_seats
            WHERE UPPER(COALESCE(status, '')) = 'ACTIVE'
            GROUP BY show_id
        ) cb ON cb.show_id = cs.show_id
        WHERE c.title = %s
          AND UPPER(COALESCE(cs.status, '')) = 'OPEN'
          AND GREATEST(0, cs.total_count - IFNULL(cb.cnt, 0)) > 0
          AND cs.show_date >= NOW()
        ORDER BY cs.show_date ASC, GREATEST(0, cs.total_count - IFNULL(cb.cnt, 0)) DESC
        LIMIT 1
        """,
        (concert_title,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit(
            f"OPEN·remain>0·show_date>=NOW() 인 회차 없음 (concert_title={concert_title!r}). Insert.sql 시드 확인."
        )
    return row


def _parse_seat_key(s: str) -> tuple[int, int] | None:
    parts = str(s or "").strip().split("-")
    if len(parts) != 2:
        return None
    try:
        r, c = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if r < 1 or c < 1:
        return None
    return (r, c)


def _ensure_user(cur, uid: int, *, dry_run: bool) -> str:
    """exists | created | will_create_on_run(dry_run 이고 없을 때)"""
    cur.execute("SELECT user_id FROM users WHERE user_id = %s", (uid,))
    if cur.fetchone():
        return "exists"
    if dry_run:
        return "will_create_on_run"
    phone = f"+1555{uid:010d}"
    if len(phone) > 20:
        phone = phone[:20]
    try:
        cur.execute(
            "INSERT INTO users (user_id, phone, password_hash, name) VALUES (%s, %s, %s, %s)",
            (uid, phone, "loadtest", f"sqs-race-{uid}"),
        )
    except IntegrityError as e:
        raise SystemExit(
            f"user_id={uid} 생성 실패(전화번호 등 유니크 충돌 가능): {e}\n"
            f"  기존 users 를 확인하거나 --user1/--user2 로 다른 id 를 쓰세요."
        ) from e
    return "created"


def _seat_is_booked(cur, show_id: int, row: int, col: int) -> bool:
    cur.execute(
        """
        SELECT 1 FROM concert_booking_seats
        WHERE show_id = %s AND seat_row_no = %s AND seat_col_no = %s AND status = 'ACTIVE'
        LIMIT 1
        """,
        (show_id, row, col),
    )
    return cur.fetchone() is not None


def _first_free_seat(cur, show_id: int, rows: int, cols: int) -> str:
    cur.execute(
        """
        SELECT seat_row_no, seat_col_no
        FROM concert_booking_seats
        WHERE show_id = %s AND status = 'ACTIVE'
        """,
        (show_id,),
    )
    booked = {(int(r["seat_row_no"]), int(r["seat_col_no"])) for r in cur.fetchall()}
    for r in range(1, rows + 1):
        for c in range(1, cols + 1):
            if (r, c) not in booked:
                return f"{r}-{c}"
    raise SystemExit("빈 좌석이 없습니다.")


def _resolve_seat_key(
    cur, show_id: int, rows: int, cols: int, seat_arg: str | None
) -> str:
    if seat_arg is None or not str(seat_arg).strip():
        return _first_free_seat(cur, show_id, rows, cols)
    parsed = _parse_seat_key(seat_arg)
    if not parsed:
        raise SystemExit(f"--seat 형식은 행-열 (예: 3-5) 입니다. 받음: {seat_arg!r}")
    r, c = parsed
    if r > rows or c > cols:
        raise SystemExit(f"좌석 {r}-{c} 는 회차 격자 {rows}x{cols} 를 벗어납니다.")
    if _seat_is_booked(cur, show_id, r, c):
        raise SystemExit(f"좌석 {r}-{c} 는 이미 ACTIVE 예매가 있습니다. 다른 --seat 또는 예약 해제 후 재시도.")
    return f"{r}-{c}"


def _user_status_ko(st: str) -> str:
    return {
        "exists": "이미 있음",
        "created": "이번 실행에서 생성함",
        "will_create_on_run": "없음 → 본 실행(비 dry-run) 시 생성",
    }.get(st, st)


def _parse_seat_csv(s: str | None) -> list[str]:
    if not s or not str(s).strip():
        return []
    out: list[str] = []
    for part in str(s).split(","):
        p = part.strip()
        if not p:
            continue
        parsed = _parse_seat_key(p)
        if not parsed:
            raise SystemExit(f"좌석 형식 오류 (행-열): {part!r}")
        out.append(f"{parsed[0]}-{parsed[1]}")
    return out


def _validate_seats_for_show(
    cur, show_id: int, rows: int, cols: int, seat_keys: list[str]
) -> None:
    for key in seat_keys:
        parsed = _parse_seat_key(key)
        if not parsed:
            raise SystemExit(f"좌석 키 오류: {key!r}")
        r, c = parsed
        if r > rows or c > cols:
            raise SystemExit(f"좌석 {key} 는 격자 {rows}x{cols} 를 벗어납니다.")
        if _seat_is_booked(cur, show_id, r, c):
            raise SystemExit(f"좌석 {key} 는 이미 ACTIVE 예매가 있어 시나리오를 시작할 수 없습니다.")


def _interleave_attempts(
    u1: int, seats1: list[str], u2: int, seats2: list[str]
) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    n = max(len(seats1), len(seats2))
    for i in range(n):
        if i < len(seats1):
            out.append((u1, seats1[i]))
        if i < len(seats2):
            out.append((u2, seats2[i]))
    return out


def _resolve_booking_redis_host() -> str:
    h = (os.getenv("ELASTICACHE_PRIMARY_ENDPOINT") or "").strip()
    if h:
        return h
    h = (os.getenv("REDIS_HOST") or "").strip()
    if h:
        return h
    ep = _terraform_output_raw("elasticache_primary_endpoint")
    if ep:
        print(
            "ELASTICACHE_PRIMARY_ENDPOINT 미설정 → terraform output elasticache_primary_endpoint 사용",
            file=sys.stderr,
        )
        return ep
    return ""


def _booking_redis_connect():
    """워커·write-api 와 동일 booking 논리 DB. 실패 시 (None, err_msg)."""
    try:
        import redis
    except ImportError:
        return None, "redis 패키지 없음 (pip install redis)"
    host = _resolve_booking_redis_host()
    if not host:
        return (
            None,
            "Redis 호스트 없음: ELASTICACHE_PRIMARY_ENDPOINT·REDIS_HOST 미설정이고 "
            "terraform output elasticache_primary_endpoint 도 실패. "
            "로컬 Redis면 export REDIS_HOST=127.0.0.1",
        )
    port = int(os.getenv("REDIS_PORT", os.getenv("ELASTICACHE_PORT", "6379")))
    db = int(os.getenv("ELASTICACHE_LOGICAL_DB_BOOKING", "1"))
    try:
        r = redis.Redis(
            host=host,
            port=port,
            db=db,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
        r.ping()
        return r, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _poll_booking_results(
    r, refs: list[str], timeout_sec: float, interval_sec: float = 0.35
) -> dict[str, dict | None]:
    pending = set(refs)
    out: dict[str, dict | None] = {ref: None for ref in refs}
    deadline = time.monotonic() + timeout_sec
    while pending and time.monotonic() < deadline:
        for ref in list(pending):
            raw = r.get(f"booking:result:{ref}")
            if raw:
                try:
                    out[ref] = json.loads(raw)
                except json.JSONDecodeError:
                    out[ref] = {"ok": False, "code": "BAD_JSON", "raw": raw[:200]}
                pending.discard(ref)
        if pending:
            time.sleep(interval_sec)
    return out


def _result_one_line(res: dict | None) -> str:
    if res is None:
        return "아직 없음(타임아웃 또는 Redis 미연결)"
    if res.get("ok"):
        return f"예매 성공 (booking_id={res.get('booking_id')}, code={res.get('code')})"
    return f"실패 (code={res.get('code')})"


def _print_outcome_report(
    *,
    user1: int,
    user2: int,
    seats_u1: list[str],
    seats_u2: list[str],
    send_order: list[tuple[int, str, str, int]],
    results: dict[str, dict | None],
) -> None:
    """
    send_order: (booking_ref, user_id, seat_key, seq_1based)
    """
    print("\n=== 시도 요약 (유저별) ===")
    print(f"  유저 {user1}: {', '.join(seats_u1) if seats_u1 else '(없음)'}")
    print(f"  유저 {user2}: {', '.join(seats_u2) if seats_u2 else '(없음)'}")

    print("\n=== SQS 전송 순서 (같은 show_id FIFO → 처리 순서와 동일) ===")
    for ref, uid, seat, seq in send_order:
        print(f"  {seq}. 유저 {uid} → 좌석 {seat}  (ref …{ref[-12:]})")

    print("\n=== 각 시도 결과 (Redis booking:result) ===")
    for ref, uid, seat, seq in send_order:
        line = _result_one_line(results.get(ref))
        print(f"  {seq}. 유저 {uid} 좌석 {seat}: {line}")

    # 좌석별: 성공한 유저 (먼저 성공한 한 명만)
    seat_winner: dict[str, int] = {}
    for ref, uid, seat, seq in send_order:
        res = results.get(ref)
        if res and res.get("ok") and seat not in seat_winner:
            seat_winner[seat] = uid

    print("\n=== 좌석별 최종 (예매 성공한 경우만) ===")
    if not seat_winner:
        print("  (성공한 예매 없음 또는 Redis 에서 결과를 못 읽음)")
    else:
        for seat in sorted(seat_winner.keys(), key=lambda x: _parse_seat_key(x) or (0, 0)):
            print(f"  좌석 {seat} → 유저 {seat_winner[seat]}")


def parse_args():
    p = argparse.ArgumentParser(description="뮤지컬: 유저2명 동시 SQS 예매 경쟁(같은 좌석)")
    p.add_argument("--user1", type=int, default=1, help="기본 1, 없으면 INSERT")
    p.add_argument("--user2", type=int, default=2, help="기본 2, 없으면 INSERT")
    p.add_argument(
        "--seat",
        default=None,
        metavar="R-C",
        help="단일 좌석 모드: 경쟁 좌석. 미지정 시 첫 빈 좌석",
    )
    p.add_argument(
        "--user1-seats",
        default=None,
        metavar="R-C,R-C,...",
        help="다좌석 겹침 모드: 유저1이 노릴 좌석 목록 ( --user2-seats 와 쌍)",
    )
    p.add_argument(
        "--user2-seats",
        default=None,
        metavar="R-C,R-C,...",
        help="다좌석 겹침 모드: 유저2 좌석 목록",
    )
    p.add_argument(
        "--concert-title",
        default=DEFAULT_MUSICAL_TITLE,
        help=f"기본: {DEFAULT_MUSICAL_TITLE!r} (시드 기준)",
    )
    p.add_argument(
        "-n",
        "--messages",
        type=int,
        default=10,
        help="단일 좌석 모드만: 병렬 메시지 수(번갈, 기본 10)",
    )
    p.add_argument(
        "--poll-sec",
        type=float,
        default=120.0,
        help="Redis booking:result 폴링 최대 대기(초), 기본 120",
    )
    p.add_argument(
        "--no-poll",
        action="store_true",
        help="전송만 하고 Redis 결과 요약 생략",
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
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--via-was",
        action="store_true",
        help="SQS 대신 POST /api/write/concerts/booking/commit (유저와 동일 경로)",
    )
    p.add_argument(
        "--write-api-base",
        default=None,
        metavar="URL",
        help="Write API 베이스 URL (미지정 시 WRITE_API_BASE_URL)",
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
    u1s = _parse_seat_csv(args.user1_seats)
    u2s = _parse_seat_csv(args.user2_seats)
    overlap_mode = bool(u1s or u2s)
    if overlap_mode:
        if not u1s or not u2s:
            raise SystemExit(
                "다좌석 모드: --user1-seats 와 --user2-seats 를 둘 다 지정하세요 (쉼표 구분 행-열)."
            )
    elif args.messages < 1:
        raise SystemExit("--messages 는 1 이상")

    queue_url = args.queue_url or _queue_url_from_terraform()

    t0 = time.monotonic()
    conn = _db_connect(_resolve_db_name(args.db_name))
    conn.autocommit(False)
    seat_key: str | None = None
    send_attempts: list[tuple[int, str]] | None = None
    try:
        try:
            with conn.cursor() as cur:
                show = _nearest_musical_show(cur, args.concert_title)
                sid = int(show["show_id"])
                rows = int(show["seat_rows"])
                cols = int(show["seat_cols"])
                st1 = _ensure_user(cur, args.user1, dry_run=args.dry_run)
                st2 = _ensure_user(cur, args.user2, dry_run=args.dry_run)
                if overlap_mode:
                    all_keys: list[str] = []
                    for k in u1s + u2s:
                        if k not in all_keys:
                            all_keys.append(k)
                    _validate_seats_for_show(cur, sid, rows, cols, all_keys)
                    send_attempts = _interleave_attempts(args.user1, u1s, args.user2, u2s)
                else:
                    seat_key = _resolve_seat_key(cur, sid, rows, cols, args.seat)
            if not args.dry_run:
                conn.commit()
            else:
                conn.rollback()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    print(
        "\n=== 경쟁 시나리오 ===\n"
        f"  공연: {args.concert_title}\n"
        f"  회차 show_id={sid}, 일시={show['show_date']}, 좌석 격자 {rows}x{cols}\n"
        f"  모드: {'다좌석 겹침' if overlap_mode else '단일 좌석'}\n"
    )
    if overlap_mode:
        print(f"  유저 {args.user1}: {_user_status_ko(st1)}")
        print(f"  유저 {args.user2}: {_user_status_ko(st2)}")
        print(f"  유저 {args.user1} 시도 좌석: {', '.join(u1s)}")
        print(f"  유저 {args.user2} 시도 좌석: {', '.join(u2s)}")
        print(f"  SQS 전송 순서: 인덱스마다 유저1·유저2 한 번씩 교차 (총 {len(send_attempts)}건)\n")
    else:
        assert seat_key is not None
        print(f"  ▶ 싸우는 좌석 (행-열): {seat_key}")
        print(f"  유저 {args.user1}: {_user_status_ko(st1)}")
        print(f"  유저 {args.user2}: {_user_status_ko(st2)}")
        print(f"  SQS 병렬 메시지 수: {args.messages} (user1/user2 번갈)\n")

    summary: dict = {
        "공연제목": args.concert_title,
        "공연_id": int(show["concert_id"]),
        "회차_id": sid,
        "회차_일시": str(show["show_date"]),
        "좌석_격자": f"{rows}x{cols}",
        "모드": "다좌석겹침" if overlap_mode else "단일좌석",
        "유저1": args.user1,
        "유저2": args.user2,
        "유저1_users상태": st1,
        "유저2_users상태": st2,
    }
    if overlap_mode:
        summary["유저1_좌석"] = u1s
        summary["유저2_좌석"] = u2s
        summary["메시지_수"] = len(send_attempts or [])
    else:
        summary["경합_좌석"] = seat_key
        summary["병렬_메시지_수"] = args.messages

    payloads: list[dict] = []
    seq = 0
    if overlap_mode:
        assert send_attempts is not None
        for uid, sk in send_attempts:
            seq += 1
            ref = str(uuid.uuid4())
            payloads.append(
                {
                    "booking_type": "concert",
                    "booking_ref": ref,
                    "user_id": uid,
                    "show_id": sid,
                    "seats": [sk],
                    "race_seq": seq,
                }
            )
    else:
        assert seat_key is not None
        for i in range(args.messages):
            uid = args.user1 if i % 2 == 0 else args.user2
            seq = i + 1
            ref = str(uuid.uuid4())
            payloads.append(
                {
                    "booking_type": "concert",
                    "booking_ref": ref,
                    "user_id": uid,
                    "show_id": sid,
                    "seats": [seat_key],
                    "race_seq": i,
                }
            )

    if args.dry_run:
        _t1 = time.monotonic()
        summary["준비_소요_초"] = round(_t1 - t0, 3)
        summary["전송_소요_초"] = 0.0
        summary["큐_소진_대기_초"] = 0.0
        summary["큐_대기_타임아웃"] = False
        summary["폴링_소요_초"] = 0.0
        summary["전체_소요_초"] = round(_t1 - t0, 3)
        summary["소요_초"] = summary["전체_소요_초"]
        summary["경로"] = "write_api" if args.via_was else "sqs"
        _remain0 = int(show.get("remain_count") or 0)
        summary["테스트후_잔여좌석"] = _remain0 - len(payloads)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print("--- dry-run 본문 샘플 ---")
        for p in payloads[:5]:
            print(json.dumps(p, ensure_ascii=False))
        print(f"... 총 {len(payloads)}건")
        print(f"\n검증: ../scripts/sql/musical_race_verify.sql 에서 SET @show_id := {sid};")
        return

    if args.via_was:
        write_base = http_w.resolve_write_api_base(args.write_api_base)
    else:
        if not queue_url:
            raise SystemExit("큐 URL 필요 (SQS) 또는 --via-was 로 Write API 사용")
        write_base = ""

    sqs = None if args.via_was else boto3.client("sqs", region_name=args.region)
    n_msg = len(payloads)
    workers = min(10, max(1, n_msg))

    def _send_one(body: dict) -> tuple[str, bool, str]:
        if args.via_was:
            uid = int(body["user_id"])
            sk = body["seats"][0]
            code, j = http_w.concert_commit(write_base, uid, sid, [sk])
            ref = str((j or {}).get("booking_ref") or "")
            if code == 200 and (j or {}).get("ok") and ref:
                return (ref, True, "")
            return (ref, False, repr(j))
        try:
            assert sqs is not None
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(body, sort_keys=True),
                MessageGroupId=f"{sid}-{int(body['user_id'])}",
            )
            return (body["booking_ref"], True, "")
        except ClientError as e:
            return (body.get("booking_ref", ""), False, str(e))

    ok = 0
    fail = 0
    done_rows: list[tuple[int, str, int, str]] = []
    t_tx_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut_map = {ex.submit(_send_one, b): b for b in payloads}
        for fut in as_completed(fut_map):
            b = fut_map[fut]
            ref, success, err = fut.result()
            if success:
                ok += 1
            else:
                fail += 1
                print(f"send 실패 ref={ref} {err}", file=sys.stderr)
            rs = int(b.get("race_seq", 0))
            done_rows.append((rs, ref, int(b["user_id"]), b["seats"][0]))
    done_rows.sort(key=lambda t: t[0])
    send_order: list[tuple[str, int, str, int]] = []
    for rs, ref, uid, sk in done_rows:
        seq_disp = rs if overlap_mode else rs + 1
        send_order.append((ref, uid, sk, seq_disp))

    t_tx_done = time.monotonic()
    prep_sec = round(t_tx_start - t0, 3)
    tx_sec = round(t_tx_done - t_tx_start, 3)

    summary["전송_성공"] = ok
    summary["전송_실패"] = fail
    summary["경로"] = "write_api" if args.via_was else "sqs"

    if not args.via_was and not args.no_wait_sqs_queue and sqs is not None:
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

    results_map: dict[str, dict | None] = {}
    poll_sec_elapsed = 0.0
    if not args.no_poll:
        r_cli, r_err = _booking_redis_connect()
        if not r_cli:
            print(
                f"\n[Redis] booking 결과를 읽지 못함: {r_err}\n"
                "  호스트: ELASTICACHE_PRIMARY_ENDPOINT / REDIS_HOST 또는 terraform elasticache_primary_endpoint. "
                "ELASTICACHE_LOGICAL_DB_BOOKING(기본 1). VPC 밖이면 ElastiCache에 붙을 수 없습니다.\n"
                "  --no-poll 로 전송만 하거나, 폴링 생략 후 DB/SQL 로 확인하세요.",
                file=sys.stderr,
            )
        else:
            # SQS: 메시지 본문의 booking_ref 가 워커·Redis 키와 동일.
            # --via-was: WAS 가 send_booking_message() 로 새 ref 를 발급하며, HTTP 본문에는
            # 클라이언트가 넣은 UUID를 보내지 않음 → payloads 의 ref 로 폴링하면 영원히 미스.
            if args.via_was:
                refs = [ref for ref, _, _, _ in send_order if ref]
            else:
                refs = [p["booking_ref"] for p in payloads]
            t_poll0 = time.monotonic()
            results_map = _poll_booking_results(r_cli, refs, float(args.poll_sec))
            poll_sec_elapsed = round(time.monotonic() - t_poll0, 3)
            if overlap_mode:
                _print_outcome_report(
                    user1=args.user1,
                    user2=args.user2,
                    seats_u1=u1s,
                    seats_u2=u2s,
                    send_order=send_order,
                    results=results_map,
                )
            else:
                assert seat_key is not None
                n1 = sum(1 for _, u, _, _ in send_order if u == args.user1)
                n2 = len(send_order) - n1
                _print_outcome_report(
                    user1=args.user1,
                    user2=args.user2,
                    seats_u1=[seat_key] * n1,
                    seats_u2=[seat_key] * n2,
                    send_order=send_order,
                    results=results_map,
                )
            brief = {}
            for ref, uid, seat, sq in send_order:
                brief[ref] = results_map.get(ref)
            summary["폴링_결과_예약별"] = brief

    t_final = time.monotonic()
    summary["준비_소요_초"] = prep_sec
    summary["전송_소요_초"] = tx_sec
    summary["폴링_소요_초"] = poll_sec_elapsed
    summary["전체_소요_초"] = round(t_final - t0, 3)
    summary["소요_초"] = summary["전체_소요_초"]
    _remain0 = int(show.get("remain_count") or 0)
    summary["테스트후_잔여좌석"] = _remain0 - len(payloads)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))

    print(f"\nDB 검증: ../scripts/sql/musical_race_verify.sql → SET @show_id := {sid};")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
