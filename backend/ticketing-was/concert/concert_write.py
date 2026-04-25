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
from sqs_client import get_booking_status_dict, send_booking_message
from concert.sale_state import get_sale_state, set_sale_state
from concert.seat_hold import adjust_remain, release_seats, try_decrease_remain_if_enough, try_hold_seats
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
    # Cognito 미들웨어가 x-cognito-sub 로부터 DB int user_id 를 resolve 하여
    # request.state.user_id 에 부착. 프론트가 payload.user_id 에 Cognito sub(UUID)
    # 문자열을 보내는 케이스에서 _to_int 가 0 으로 떨어져 400/500 이 나던 버그 방지.
    user_id = _to_int(getattr(request.state, "user_id", None) or data.get("user_id"))
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

    # Concert6 방향(라우트 추가 없이): commit에서 DB 선검사를 제거하고,
    # Redis remain(단일 카운터) + Redis hold(주황)로 "접수 즉시 반영"을 우선한다.
    # remain 카운터가 없으면 1회 seed (reset 직후 등)
    _seed_remain_count_if_missing(show_id)

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
        # 1) remain 선차감(충분할 때만) — 즉시 매진 처리(주황/백그라운드 연출의 핵심)
        ok_remain, remain_after = try_decrease_remain_if_enough(show_id=show_id, count=req_count)
        if not ok_remain:
            return JSONResponse(status_code=409, content={"ok": False, "code": "SOLD_OUT", "remain": remain_after})

        # 2) 홀드(주황) 경로: 좌석 단위 선점. remain은 이미 차감했으므로 여기서는 차감하지 않는다.
        try:
            hold = try_hold_seats(
                show_id=show_id,
                seats=parsed_seats,
                booking_ref=booking_ref,
                adjust_remain_count=False,
            )
        except Exception:
            # 홀드 경로는 Redis(any_confirmed / SET NX / hold set)만 사용한다.
            # Redis 타임아웃·연결 끊김 등도 여기로 모이므로 pymysql 한정이면 잡히지 않는다.
            log.exception("commit_concert_booking: hold path failed (Redis/내부 오류) show_id=%s", show_id)
            adjust_remain(show_id=show_id, delta=req_count)
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
            # remain은 선차감했으니 실패 시 복구
            adjust_remain(show_id=show_id, delta=req_count)
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
        else:
            # skip_hold=true인데 remain을 선차감하지 않았으므로 별도 복구 없음
            pass
        raise
    return {
        "ok": True,
        "code": "QUEUED",
        "booking_ref": booking_ref,
        "message": "예매 요청이 접수되었습니다.",
    }


@router.post("/api/write/concerts/{show_id}/waiting-room/enter")
def enter_waiting_room(show_id: int, payload: dict):
    data = payload if isinstance(payload, dict) else {}
    user_id = _to_int(data.get("user_id"))
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
