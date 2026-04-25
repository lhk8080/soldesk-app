"""
극장 예매 쓰기 — SQS FIFO 통합 버전.

MessageGroupId=schedule_id-user_id 로 유저별 FIFO 파이프를 나눈다.
실제 좌석·잔여 처리는 worker-svc 가 DB에서 좌석 유니크 + 원자적 잔여 UPDATE 로 수행한다.
"""
import json
import secrets
import string

import pymysql
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from sqs_client import get_booking_status_dict, send_booking_message

router = APIRouter()


# NOTE: Local synchronous fallback removed.


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
    return f"{letters}{digits}"


@router.post("/api/write/theaters/booking/commit")
def commit_booking(payload: dict, request: Request):
    data = payload if isinstance(payload, dict) else {}
    user_id = _to_int(getattr(request.state, "user_id", None) or data.get("user_id"))
    schedule_id = _to_int(data.get("schedule_id"))
    seats = data.get("seats") or []

    if user_id <= 0 or schedule_id <= 0:
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

    booking_ref = send_booking_message(
        booking_type="theater",
        group_id=f"{schedule_id}-{user_id}",
        payload={
            "user_id": user_id,
            "schedule_id": schedule_id,
            "seats": [f"{r}-{c}" for r, c in parsed_seats],
        },
    )
    return {
        "ok": True,
        "code": "QUEUED",
        "booking_ref": booking_ref,
        "message": "예매 요청이 접수되었습니다. 잠시 후 결과를 확인해주세요.",
    }


@router.get("/api/write/booking/status/{booking_ref}")
def check_booking_status(booking_ref: str):
    """SQS 비동기 예매 결과 조회 (프론트엔드에서 폴링). Redis queued 키로 PROCESSING vs 무효 ref 구분."""
    return get_booking_status_dict(booking_ref)
