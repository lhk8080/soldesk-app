"""
콘서트 예매 쓰기 — SQS FIFO 통합 버전.
원본: _ShowLockPool(threading.Lock) → SQS FIFO MessageGroupId=show_id.
      유저별 그룹(show_id-user_id)으로 분리해 동일 회차라도 타 유저 대량 적체에 GUI 예매가 묻히지 않게 함(DB는 FOR UPDATE 로 좌석 정합성 유지).
"""
import json
import logging
import uuid
import secrets
import string
import pymysql
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import BOOKING_QUEUE_COUNTER_TTL_SEC, DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from config import CACHE_ENABLED
from sqs_client import get_booking_status_dict, send_booking_message
from concert.sale_state import get_sale_state, set_sale_state
from concert.seat_hold import release_seats, try_hold_seats
from cache.redis_client import redis_client
from waiting_room import (
    enter as wr_enter,
    metrics as wr_metrics,
    observe as wr_observe,
    reset as wr_reset,
    set_control_full as wr_set_control_full,
    status as wr_status,
    verify_permit as wr_verify_permit,
)

router = APIRouter()

log = logging.getLogger(__name__)

# NOTE: Local synchronous fallback removed (EKS-only).

def _remain_count_key(show_id: int) -> str:
    # remain_count 단일 카운터(단일 진실)
    # 주의: Redis key suffix는 레거시 호환을 위해 ':remain:v1'를 유지한다.
    return f"concert:show:{int(show_id)}:remain:v1"


def _seed_remain_count_if_missing(show_id: int) -> int:
    """
    remain_count Redis 카운터는 write 경로에서 INCRBY로 조정되므로, 키가 없으면 음수 클램프(0)로 매진이 될 수 있다.
    따라서 키가 없을 때만 DB remain_count로 1회 seed(setnx)한다.
    Returns: seed에 사용한 값(또는 기존 값 파싱 실패 시 0)
    """
    sid = int(show_id or 0)
    if sid <= 0:
        return 0
    k = _remain_count_key(sid)
    raw_val: int | None = None
    try:
        raw = redis_client.get(k)
        if raw is not None:
            raw_val = max(0, int(raw or 0))
            # 과거 버그로 remain key가 0으로 "굳은" 경우를 복구:
            # hold/confirmed/pending이 모두 0이면 아직 판매/점유가 없다는 뜻이므로 DB seed로 복원해도 안전하다.
            if raw_val <= 0:
                try:
                    hold_n = int(redis_client.scard(f"concert:hold:{sid}:v1") or 0)
                except Exception:
                    hold_n = 0
                try:
                    conf_n = int(redis_client.scard(f"concert:confirmed:{sid}:v1") or 0)
                except Exception:
                    conf_n = 0
                try:
                    pending_n = max(0, int(redis_client.get(f"concert:show:{sid}:pending:v1") or 0))
                except Exception:
                    pending_n = 0
                if hold_n > 0 or conf_n > 0 or pending_n > 0:
                    return int(raw_val)
                # no activity -> fall through to DB seed/repair below
            else:
                return int(raw_val)
    except Exception:
        raw_val = None

    # 키가 없거나, "0으로 굳은" 상태(활동 없음)일 때만 DB에서 가져와 seed/repair
    seed = 0
    conn = _get_tx_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT remain_count FROM concert_shows WHERE show_id = %s LIMIT 1",
                (sid,),
            )
            row = cur.fetchone()
            seed = max(0, int((row or {}).get("remain_count") or 0))
    except Exception:
        seed = 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        if seed > 0:
            # raw_val is None -> seed with setnx
            if raw_val is None:
                redis_client.setnx(k, int(seed))
            else:
                # repair stuck-0 key (no activity) -> overwrite
                redis_client.set(k, int(seed))
    except Exception:
        pass
    return int(seed)


def _reset_concert_redis_seat_state(*, show_id: int) -> dict:
    """
    데모/테스트용: 해당 show_id의 '좌석 상태' Redis 키를 **write-api가 실제로 사용하는 Redis(DB=cache)** 기준으로 리셋.
    - confirmed/hold/hold_rev/pending/show snapshot/seat hold keys 정리
    - holdmeta는 booking_ref 기준 키라 show_id별 패턴이 없어, value(JSON)에서 show_id를 확인해 선택적으로 삭제한다.
    """
    sid = int(show_id or 0)
    if sid <= 0:
        return {"ok": False, "code": "BAD_SHOW_ID"}

    keys_fixed = [
        f"concert:confirmed:{sid}:v1",
        f"concert:hold:{sid}:v1",
        f"concert:show:{sid}:hold_rev:v1",
        # remain 단일 카운터(단일 진실) — 데모/테스트 리셋 시 반드시 초기화해야 함.
        # (안 지우면 이전 런의 값(예: 50000)이 남아 booking-holds에서 잔여가 튀는 현상이 생길 수 있다.)
        f"concert:show:{sid}:remain:v1",
        f"concert:show:{sid}:pending:v1",
        f"concert:show:{sid}:read:v2",
    ]
    deleted_fixed = 0
    try:
        deleted_fixed = int(redis_client.delete(*keys_fixed) or 0)
    except Exception:
        deleted_fixed = 0

    # seat hold keys는 개수가 많을 수 있어 scan_iter로 정리
    deleted_seat_keys = 0
    try:
        batch: list[str] = []
        pat = f"concert:seat:{sid}:*:hold:v1"
        for k in redis_client.scan_iter(match=pat, count=1000):
            batch.append(str(k))
            if len(batch) >= 1000:
                deleted_seat_keys += int(redis_client.delete(*batch) or 0)
                batch = []
        if batch:
            deleted_seat_keys += int(redis_client.delete(*batch) or 0)
    except Exception:
        deleted_seat_keys = 0

    # holdmeta는 show_id별 패턴이 없어 value를 보고 골라서 삭제(데모 전용)
    deleted_holdmeta = 0
    scanned_holdmeta = 0
    try:
        batch2: list[str] = []
        for k in redis_client.scan_iter(match="concert:holdmeta:*:v1", count=1000):
            kk = str(k)
            scanned_holdmeta += 1
            try:
                raw = redis_client.get(kk)
                meta = json.loads(raw) if raw else None
                if isinstance(meta, dict) and int(meta.get("show_id") or 0) == sid:
                    batch2.append(kk)
            except Exception:
                # 파싱 실패 등은 스킵
                continue
            if len(batch2) >= 500:
                deleted_holdmeta += int(redis_client.delete(*batch2) or 0)
                batch2 = []
        if batch2:
            deleted_holdmeta += int(redis_client.delete(*batch2) or 0)
    except Exception:
        deleted_holdmeta = 0

    # reset 직후 remain_count는 "삭제"만 하면 첫 커밋에서 0으로 클램프될 수 있으므로,
    # DB 값으로 seed 해 두어 데모가 1장만 사도 매진되는 문제를 방지한다.
    seeded_remain = _seed_remain_count_if_missing(sid)

    return {
        "ok": True,
        "show_id": sid,
        "deleted_fixed": int(deleted_fixed),
        "deleted_seat_keys": int(deleted_seat_keys),
        "deleted_holdmeta": int(deleted_holdmeta),
        "scanned_holdmeta": int(scanned_holdmeta),
        "seeded_remain_count": int(seeded_remain),
    }


def _reconcile_concert_redis_state_from_db(*, show_id: int) -> dict:
    """
    개발/재배포 시 DB를 기준으로 Redis(좌석 상태/잔여/confirmed)를 재구축한다.
    목적:
    - Redis에 남아있는 CONFIRMED/hold/remain 등이 DB와 불일치해 "빈좌석/매진"이 잘못 보이는 문제를 해소

    동작:
    - (1) Redis의 show_id 관련 좌석 상태 키들을 정리(reset과 유사)
    - (2) DB ACTIVE 좌석을 조회해 Redis confirmed set을 재작성
    - (3) DB total - confirmed - (DB holds) 로 remain을 계산해 Redis remain 카운터를 overwrite
    """
    sid = int(show_id or 0)
    if sid <= 0:
        return {"ok": False, "code": "BAD_SHOW_ID"}

    # 1) 먼저 Redis 키들을 정리
    base = _reset_concert_redis_seat_state(show_id=sid)
    if not base.get("ok"):
        return base

    total = 0
    confirmed_keys: list[str] = []
    holds_n = 0
    conn = _get_tx_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT total_count FROM concert_shows WHERE show_id=%s LIMIT 1", (sid,))
            row0 = cur.fetchone() or {}
            total = int(row0.get("total_count") or 0)

            cur.execute(
                "SELECT seat_row_no, seat_col_no FROM concert_booking_seats "
                "WHERE show_id=%s AND UPPER(COALESCE(status,''))='ACTIVE' "
                "ORDER BY seat_row_no ASC, seat_col_no ASC",
                (sid,),
            )
            rows = cur.fetchall() or []
            confirmed_keys = [f"{int(r['seat_row_no'])}-{int(r['seat_col_no'])}" for r in rows]

            # DB holds(폴백 테이블)가 남아있을 수 있음(과거 Redis OFF 실행/테스트)
            try:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM concert_seat_holds "
                    "WHERE show_id=%s AND (expires_at IS NULL OR expires_at > NOW())",
                    (sid,),
                )
                holds_n = int((cur.fetchone() or {}).get("n") or 0)
            except Exception:
                holds_n = 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 2) confirmed set 재작성
    confirmed_added = 0
    try:
        if confirmed_keys:
            sk = f"concert:confirmed:{sid}:v1"
            pipe = redis_client.pipeline()
            pipe.delete(sk)
            pipe.sadd(sk, *[str(x) for x in confirmed_keys])
            pipe.execute()
            confirmed_added = len(confirmed_keys)
    except Exception:
        confirmed_added = 0

    # 3) remain 카운터 overwrite (DB 기반 계산)
    try:
        remain = max(0, int(total) - int(len(confirmed_keys)) - int(holds_n))
    except Exception:
        remain = 0
    try:
        redis_client.set(f"concert:show:{sid}:remain:v1", int(remain))
    except Exception:
        pass

    out = dict(base)
    out.update(
        {
            "reconciled": True,
            "total_count_db": int(total),
            "confirmed_from_db": int(len(confirmed_keys)),
            "holds_from_db": int(holds_n),
            "confirmed_set_written": int(confirmed_added),
            "remain_written": int(remain),
        }
    )
    return out

def _pending_key(show_id: int) -> str:
    return f"concert:show:{int(show_id)}:pending:v1"


def _pending_incr(show_id: int, delta: int) -> None:
    d = int(delta or 0)
    if d == 0:
        return


def _db_pending_adjust(show_id: int, delta: int) -> None:
    # Reverted: pending_count DB column is not used.
    return


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_seat_key(value: str):
    text = str(value or "").strip()
    parts = text.split("-")
    if len(parts) != 2:
        return None
    row = _to_int(parts[0])
    col = _to_int(parts[1])
    if row <= 0 or col <= 0:
        return None
    return row, col


def _get_tx_connection():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _db_any_active_seat(*, show_id: int, seats: list[tuple[int, int]]) -> bool:
    """
    DB 단일 진실: 이미 ACTIVE(확정) 좌석이 있으면 write 단계에서 조기 차단한다.
    - confirmed set/스냅샷이 reset 등으로 비어 있어도, DB는 항상 최종 근거다.
    - DB 조회 실패 시 False를 돌려 실패를 숨기면( fail-open ) 홀드·QUEUED 후 워커에서만 DUPLICATE_SEAT가
      대량 발생할 수 있으므로, 예외는 로깅 후 그대로 전파한다.
    """
    sid = int(show_id or 0)
    if sid <= 0 or not seats:
        return False
    conn = None
    try:
        conn = _get_tx_connection()
        with conn.cursor() as cur:
            clauses = []
            params: list[int] = [sid]
            for r, c in seats:
                clauses.append("(seat_row_no=%s AND seat_col_no=%s)")
                params.extend([int(r), int(c)])
            where_seats = " OR ".join(clauses) if clauses else "1=0"
            cur.execute(
                "SELECT 1 FROM concert_booking_seats "
                "WHERE show_id=%s AND UPPER(COALESCE(status,''))='ACTIVE' AND ("
                + where_seats +
                ") LIMIT 1",
                tuple(params),
            )
            return bool(cur.fetchone())
    except pymysql.err.Error:
        log.exception("concert write: ACTIVE seat check failed (DB) show_id=%s", sid)
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _db_show_snapshot(*, show_id: int) -> dict | None:
    conn = _get_tx_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT show_id, seat_rows, seat_cols, total_count, remain_count, status "
                "FROM concert_shows WHERE show_id=%s LIMIT 1",
                (int(show_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _db_remaining_including_holds(*, show_id: int) -> int:
    """
    Redis OFF 모드에서 정확한 잔여를 계산하기 위한 DB 기반 remain:
      remain = total_count - confirmed(ACTIVE) - holds(active)
    """
    sid = int(show_id or 0)
    if sid <= 0:
        return 0
    conn = _get_tx_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT total_count FROM concert_shows WHERE show_id=%s LIMIT 1",
                (sid,),
            )
            r0 = cur.fetchone() or {}
            total = int(r0.get("total_count") or 0)
            cur.execute(
                "SELECT COUNT(*) AS n FROM concert_booking_seats "
                "WHERE show_id=%s AND UPPER(COALESCE(status,''))='ACTIVE'",
                (sid,),
            )
            confirmed = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute(
                "SELECT COUNT(*) AS n FROM concert_seat_holds "
                "WHERE show_id=%s AND (expires_at IS NULL OR expires_at > NOW())",
                (sid,),
            )
            holds = int((cur.fetchone() or {}).get("n") or 0)
            return max(0, total - confirmed - holds)
    except Exception:
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _is_duplicate_key_error(exc: Exception) -> bool:
    if not isinstance(exc, pymysql.err.IntegrityError):
        return False
    try:
        return int(exc.args[0]) == 1062
    except Exception:
        return False


def _generate_booking_code() -> str:
    letters = "".join(secrets.choice(string.ascii_uppercase) for _ in range(2))
    digits = "".join(secrets.choice(string.digits) for _ in range(6))
    return f"C{letters}{digits}"


def _seat_shard_id(row: int, col: int) -> int:
    """
    한 회차 내부 샤딩(병렬 처리 데모/운영용).
    - FIFO MessageGroupId를 show_id 단일이 아니라 show_id+shard로 쪼개면
      같은 회차라도 서로 다른 shard는 병렬 처리 가능(파드 확장 효과가 큼).
    - 샤드 수는 환경변수로 조절(기본 64).
    """
    try:
        n = int((__import__("os").getenv("CONCERT_FIFO_SHARDS", "64") or "64").strip())
    except Exception:
        n = 64
    n = max(1, min(1024, n))
    # 좌석 좌표 기반의 간단한 해시(빠르고 분산 충분)
    return ((int(row) * 1000003) ^ int(col)) % n


@router.post("/api/write/concerts/booking/commit")
def commit_concert_booking(payload: dict, request: Request):
    data = payload if isinstance(payload, dict) else {}
    # user_id 는 인증 미들웨어 신뢰 (payload 값 무시 — 위조 방지).
    user_id = _to_int(getattr(request.state, "user_id", 0))
    show_id = _to_int(data.get("show_id"))
    seats = data.get("seats") or []
    skip_hold = bool(data.get("skip_hold") is True)
    permit_token = str(data.get("permit_token") or "").strip()
    queue_ref = str(data.get("queue_ref") or "").strip()

    if user_id <= 0 or show_id <= 0:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "code": "BAD_REQUEST", "message": "요청값이 올바르지 않습니다."},
        )

    if not isinstance(seats, list) or not seats:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "code": "NO_SEATS", "message": "좌석을 선택해주세요."},
        )

    parsed_seats = []
    seat_set = set()
    for item in seats:
        parsed = _parse_seat_key(item)
        if not parsed:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "code": "BAD_SEAT_KEY", "message": "좌석 형식이 올바르지 않습니다."},
            )
        if parsed in seat_set:
            continue
        seat_set.add(parsed)
        parsed_seats.append(parsed)

    req_count = len(parsed_seats)
    if req_count <= 0:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "code": "NO_SEATS", "message": "좌석을 선택해주세요."},
        )

    # DB 단일 진실 기반의 선검사(조기 실패):
    # - remain_count가 부족하면 SOLD_OUT
    # - 이미 ACTIVE 좌석이면 DUPLICATE_SEAT
    show_row = _db_show_snapshot(show_id=show_id)
    if not show_row:
        return JSONResponse(status_code=404, content={"ok": False, "code": "NOT_FOUND"})
    try:
        seat_rows = int(show_row.get("seat_rows") or 0)
        seat_cols = int(show_row.get("seat_cols") or 0)
    except Exception:
        seat_rows, seat_cols = 0, 0
    for r, c in parsed_seats:
        if seat_rows > 0 and r > seat_rows:
            return JSONResponse(status_code=409, content={"ok": False, "code": "INVALID_SEAT"})
        if seat_cols > 0 and c > seat_cols:
            return JSONResponse(status_code=409, content={"ok": False, "code": "INVALID_SEAT"})
    try:
        remain_db = int(show_row.get("remain_count") or 0)
    except Exception:
        remain_db = 0
    # Redis OFF(CACHE_ENABLED=false)에서는 DB remain_count 컬럼이 "홀드 포함 정확값"이 아닐 수 있어,
    # DB에서 confirmed+holds 기반으로 다시 계산한다.
    if not CACHE_ENABLED:
        remain_db = int(_db_remaining_including_holds(show_id=show_id))
    if remain_db < req_count:
        return JSONResponse(status_code=409, content={"ok": False, "code": "SOLD_OUT"})
    try:
        if _db_any_active_seat(show_id=show_id, seats=parsed_seats):
            return JSONResponse(status_code=409, content={"ok": False, "code": "DUPLICATE_SEAT"})
    except pymysql.err.Error:
        log.exception("commit_concert_booking: DB error during active seat check show_id=%s", show_id)
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "code": "DB_UNAVAILABLE",
                "message": "일시적으로 예매 검증을 수행할 수 없습니다. 잠시 후 다시 시도해주세요.",
            },
        )

    # Waiting Room(입장 대기열): permit 없이는 커밋을 받지 않는다(새치기 방지).
    if not wr_verify_permit(permit_token=permit_token, kind="concert", entity_id=show_id, user_id=user_id):
        # UX: 좌석 중복 등으로 재시도하는 유저는 "이미 ADMITTED" 상태일 수 있다.
        # permit TTL이 짧아 만료된 경우, queue_ref를 함께 받으면 status()로 즉시 새 permit을 발급받아 우선 처리한다.
        if queue_ref:
            try:
                st = wr_status(queue_ref=queue_ref)
                if isinstance(st, dict) and st.get("status") == "ADMITTED" and st.get("permit_token"):
                    permit_token2 = str(st.get("permit_token") or "").strip()
                    if permit_token2 and wr_verify_permit(
                        permit_token=permit_token2, kind="concert", entity_id=show_id, user_id=user_id
                    ):
                        permit_token = permit_token2
                    else:
                        raise ValueError("permit verify failed")
                else:
                    raise ValueError("not admitted")
            except Exception:
                return JSONResponse(
                    status_code=429,
                    content={
                        "ok": False,
                        "code": "WAITING_ROOM_REQUIRED",
                        "message": "대기열 처리 중입니다. 잠시 후 다시 시도해주세요.",
                    },
                )
        else:
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "code": "WAITING_ROOM_REQUIRED",
                    "message": "대기열 처리 중입니다. 잠시 후 다시 시도해주세요.",
                },
            )

    # booking_ref를 write-api에서 먼저 발급해 Redis 홀드 ↔ SQS 메시지 ↔ 폴링 키를 하나로 묶는다.
    booking_ref = str(uuid.uuid4())

    hold_applied = False
    if skip_hold:
        # "바로 큐로" 경로 (레거시/테스트): 홀드를 걸지 않고 큐에 넣는다.
        pass
    else:
        # 1) 홀드(주황) 경로: 좌석 단위 선점으로 remain이 즉시 줄어들게 한다.
        try:
            hold = try_hold_seats(
                show_id=show_id, seats=parsed_seats, booking_ref=booking_ref
            )
        except pymysql.err.Error:
            log.exception("commit_concert_booking: DB error during hold/confirmed check show_id=%s", show_id)
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "code": "DB_UNAVAILABLE",
                    "message": "일시적으로 예매 검증을 수행할 수 없습니다. 잠시 후 다시 시도해주세요.",
                },
            )
        if not hold.get("ok"):
            code = hold.get("code") or "DUPLICATE_SEAT"
            # confirmed 좌석은 hold로 덮을 수 없음(회색->주황 금지)
            if str(code) == "CONFIRMED_SEAT":
                return JSONResponse(status_code=409, content={"ok": False, "code": "DUPLICATE_SEAT"})
            return JSONResponse(status_code=409, content={"ok": False, "code": code})
        hold_applied = True

    # Read API는 show 스냅샷 캐시(concert:show:{id}:read:v2)를 그대로 재사용한다.
    # 큐 적체가 길어도 "접수(홀드)" 좌석이 즉시 UI에 점유로 보이려면,
    # 홀드 성공 시점에 스냅샷을 무효화해 다음 조회에서 reserved_seats_snapshot 기반으로 재계산되게 해야 한다.
    try:
        redis_client.delete(f"concert:show:{int(show_id)}:read:v2")
    except Exception:
        pass

    try:
        booking_ref = send_booking_message(
            booking_type="concert",
            group_id=f"{show_id}-sh{_seat_shard_id(parsed_seats[0][0], parsed_seats[0][1])}",
            booking_ref=booking_ref,
            payload={
                "user_id": user_id,
                "show_id": show_id,
                "seats": [f"{r}-{c}" for r, c in parsed_seats],
                "hold_applied": bool(hold_applied),
            },
        )
    except Exception:
        # enqueue 실패는 접수 자체가 실패이므로 hold를 즉시 원복한다.
        if hold_applied:
            release_seats(show_id=show_id, seats=parsed_seats, booking_ref=booking_ref)
        raise
    return {
        "ok": True,
        "code": "QUEUED",
        "booking_ref": booking_ref,
        "message": "예매 요청이 접수되었습니다.",
    }


@router.post("/api/write/concerts/{show_id}/waiting-room/enter")
def enter_waiting_room(show_id: int, request: Request):
    # 인증된 user_id 는 미들웨어가 request.state 에 세팅 (payload 값은 위조 가능하므로 신뢰 금지).
    user_id = _to_int(getattr(request.state, "user_id", 0))
    if user_id <= 0 or int(show_id) <= 0:
        return JSONResponse(status_code=400, content={"ok": False, "code": "BAD_REQUEST"})
    return wr_enter(kind="concert", entity_id=int(show_id), user_id=user_id)


@router.post("/api/write/concerts/{show_id}/waiting-room/reset")
def reset_waiting_room(show_id: int):
    # 테스트/데모용: 서버가 실제로 사용하는 Redis 기준으로 WR 카운터 리셋
    return wr_reset(kind="concert", entity_id=int(show_id))

@router.post("/api/write/concerts/{show_id}/redis/reset")
def reset_concert_redis(show_id: int):
    # 테스트/데모용: 서버가 실제로 쓰는 Redis(cache 논리 DB) 기준으로 콘서트 좌석 상태 키 리셋
    return _reset_concert_redis_seat_state(show_id=int(show_id))


@router.post("/api/write/concerts/{show_id}/redis/reconcile")
def reconcile_concert_redis(show_id: int):
    """
    개발/재배포 시 DB 기준으로 Redis 좌석 상태를 재구축.
    - reset보다 강함(confirmed set + remain까지 DB로부터 재작성)
    """
    return _reconcile_concert_redis_state_from_db(show_id=int(show_id))


@router.get("/api/write/concerts/waiting-room/status/{queue_ref}")
def waiting_room_status(queue_ref: str):
    return wr_status(queue_ref=queue_ref)


@router.get("/api/write/concerts/{show_id}/waiting-room/metrics")
def waiting_room_metrics(show_id: int):
    return wr_metrics(kind="concert", entity_id=int(show_id))


@router.post("/api/write/concerts/{show_id}/waiting-room/control")
def waiting_room_control(show_id: int, payload: dict):
    data = payload if isinstance(payload, dict) else {}
    mode = data.get("mode", None)
    if mode is not None:
        mode = str(mode).strip().upper()
        if mode not in ("AUTO", "MANUAL"):
            return JSONResponse(status_code=400, content={"ok": False, "code": "BAD_MODE"})
    enabled = data.get("enabled", None)
    if enabled is not None:
        enabled = bool(enabled)
    rate = data.get("admit_rate_per_sec", None)
    if rate is not None:
        try:
            rate = int(rate)
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "code": "BAD_RATE"})
    message = data.get("message", None)
    return wr_set_control_full(
        kind="concert",
        entity_id=int(show_id),
        mode=mode,
        enabled=enabled,
        admit_rate_per_sec=rate,
        message=message,
    )


@router.post("/api/write/concerts/{show_id}/waiting-room/observe")
def waiting_room_observe(show_id: int, payload: dict):
    data = payload if isinstance(payload, dict) else {}
    return wr_observe(kind="concert", entity_id=int(show_id), data=data)


@router.get("/api/write/concerts/booking/status/{booking_ref}")
def check_concert_booking_status(booking_ref: str):
    return get_booking_status_dict(booking_ref)


# --- 판매 상태 제어(운영/데모용) ---
@router.get("/api/write/concerts/{show_id}/sale")
def get_concert_sale(show_id: int):
    return {"ok": True, "show_id": int(show_id), "sale": get_sale_state(int(show_id))}


@router.post("/api/write/concerts/{show_id}/sale/open")
def open_concert_sale(show_id: int):
    set_sale_state(int(show_id), "OPEN")
    return {"ok": True, "show_id": int(show_id), "sale": get_sale_state(int(show_id))}


@router.post("/api/write/concerts/{show_id}/sale/close")
def close_concert_sale(show_id: int):
    set_sale_state(int(show_id), "CLOSED")
    return {"ok": True, "show_id": int(show_id), "sale": get_sale_state(int(show_id))}
