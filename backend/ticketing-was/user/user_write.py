"""
사용자 예매 환불 (극장 / 콘서트)
"""
from typing import Any, Dict, Optional

import pymysql
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER

router = APIRouter()


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def _refund_movie_booking(user_id: int, booking_id: int):
    """극장(영화) 예매 환불: booking→CANCEL, payment→N, 좌석 반환, 캐시 무효화."""
    from theater.theaters_read import warmup_theaters_booking_caches

    conn = _get_tx_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT booking_id, user_id, schedule_id, reg_count, book_status "
                "FROM booking WHERE booking_id = %s AND user_id = %s FOR UPDATE",
                (booking_id, user_id),
            )
            booking = cur.fetchone()
            if not booking:
                conn.rollback()
                return JSONResponse(
                    status_code=404,
                    content={"ok": False, "message": "예매 내역을 찾을 수 없습니다."},
                )

            if (booking.get("book_status") or "").upper() in ("CANCEL", "CANCELED", "CANCELLED"):
                conn.rollback()
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "message": "이미 취소된 예매입니다."},
                )

            schedule_id = _to_int(booking.get("schedule_id"))
            reg_count = _to_int(booking.get("reg_count"))

            cur.execute(
                "UPDATE booking SET book_status = 'CANCEL' WHERE booking_id = %s",
                (booking_id,),
            )

            cur.execute(
                "UPDATE payment SET pay_yn = 'N' WHERE booking_id = %s",
                (booking_id,),
            )

            cur.execute(
                "UPDATE booking_seats SET status = 'CANCEL' WHERE booking_id = %s AND status = 'ACTIVE'",
                (booking_id,),
            )

            # 영화 환불 시 schedules.remain_count 복원 (026322b) — 콘서트와 달리 worker-svc 가
            # remain 을 역전파하는 async 루프가 없어 동기 UPDATE 가 필요.
            if schedule_id > 0 and reg_count > 0:
                cur.execute(
                    "UPDATE schedules SET remain_count = remain_count + %s WHERE schedule_id = %s",
                    (reg_count, schedule_id),
                )

        conn.commit()
    except Exception as exc:
        conn.rollback()
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": str(exc)},
        )
    finally:
        conn.close()

    try:
        warmup_theaters_booking_caches()
    except Exception:
        pass

    return {"ok": True, "message": "환불이 완료되었습니다."}


def _refund_concert_booking(user_id: int, booking_id: int):
    """콘서트 예매 환불: concert_booking→CANCEL, concert_payment→N, 좌석 반환."""
    concert_id_for_cache = 0
    show_id_for_cache = 0
    refunded_seat_keys: list[str] = []
    remain_after_db: int | None = None
    conn = _get_tx_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT booking_id, user_id, show_id, reg_count, book_status "
                "FROM concert_booking WHERE booking_id = %s AND user_id = %s FOR UPDATE",
                (booking_id, user_id),
            )
            booking = cur.fetchone()
            if not booking:
                conn.rollback()
                return JSONResponse(
                    status_code=404,
                    content={"ok": False, "message": "예매 내역을 찾을 수 없습니다."},
                )

            if (booking.get("book_status") or "").upper() in ("CANCEL", "CANCELED", "CANCELLED"):
                conn.rollback()
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "message": "이미 취소된 예매입니다."},
                )

            show_id = _to_int(booking.get("show_id"))
            show_id_for_cache = show_id
            reg_count = _to_int(booking.get("reg_count"))

            if show_id > 0:
                cur.execute(
                    "SELECT concert_id FROM concert_shows WHERE show_id = %s LIMIT 1",
                    (show_id,),
                )
                crow = cur.fetchone()
                if crow:
                    concert_id_for_cache = _to_int(crow.get("concert_id"))

            # 환불로 취소되는 ACTIVE 좌석을 미리 수집(confirmed set/remain 카운터 복구용)
            if show_id > 0:
                cur.execute(
                    "SELECT seat_row_no, seat_col_no FROM concert_booking_seats "
                    "WHERE booking_id=%s AND UPPER(COALESCE(status,''))='ACTIVE' FOR UPDATE",
                    (booking_id,),
                )
                rows = cur.fetchall() or []
                refunded_seat_keys = [
                    f"{_to_int(r.get('seat_row_no'))}-{_to_int(r.get('seat_col_no'))}"
                    for r in rows
                    if _to_int(r.get("seat_row_no")) > 0 and _to_int(r.get("seat_col_no")) > 0
                ]

            cur.execute(
                "UPDATE concert_booking SET book_status = 'CANCEL' WHERE booking_id = %s",
                (booking_id,),
            )

            cur.execute(
                "UPDATE concert_payment SET pay_yn = 'N' WHERE booking_id = %s",
                (booking_id,),
            )

            cur.execute(
                "UPDATE concert_booking_seats SET status = 'CANCEL' WHERE booking_id = %s AND status = 'ACTIVE'",
                (booking_id,),
            )

            # DB remain_count/status 동기화: ACTIVE 좌석 수 기반으로 재계산
            if show_id > 0:
                cur.execute(
                    """
                    UPDATE concert_shows cs
                    SET cs.remain_count = GREATEST(
                        0,
                        cs.total_count - (
                            SELECT COUNT(*)
                            FROM concert_booking_seats cbs
                            WHERE cbs.show_id = cs.show_id
                              AND UPPER(COALESCE(cbs.status, '')) = 'ACTIVE'
                        )
                    )
                    WHERE cs.show_id = %s
                    """,
                    (int(show_id),),
                )
                cur.execute("SELECT remain_count FROM concert_shows WHERE show_id = %s", (int(show_id),))
                row = cur.fetchone() or {}
                remain_after = _to_int(row.get("remain_count"))
                remain_after_db = int(remain_after)
                if remain_after <= 0:
                    cur.execute("UPDATE concert_shows SET status = 'CLOSED' WHERE show_id = %s", (int(show_id),))
                else:
                    cur.execute("UPDATE concert_shows SET status = 'OPEN' WHERE show_id = %s", (int(show_id),))

        conn.commit()
    except Exception as exc:
        conn.rollback()
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": str(exc)},
        )
    finally:
        conn.close()

    try:
        from concert.concert_read_cache import invalidate_concert_caches_after_booking
        from cache.redis_client import redis_client
        from concert.seat_hold import release_seats_on_refund, remove_confirmed_seats

        if concert_id_for_cache > 0:
            invalidate_concert_caches_after_booking(
                concert_id_for_cache,
                show_id=show_id_for_cache if show_id_for_cache > 0 else None,
            )

        # Redis 정합성 보정(best-effort):
        # - confirmed set에서 환불 좌석 제거(중복좌석 1차 가드)
        # - seat 키(concert:seat:*)와 hold set도 정리(any_confirmed 0차 가드의 "CONFIRMED" 문자열 잔재 제거, 7433228)
        # - remain 단일 카운터를 DB 값으로 동기화(잔여 복구)
        if show_id_for_cache > 0:
            if refunded_seat_keys:
                remove_confirmed_seats(show_id=int(show_id_for_cache), seat_keys=refunded_seat_keys)
                seats_tuples: list[tuple[int, int]] = []
                for k in refunded_seat_keys:
                    try:
                        r_str, c_str = str(k).split("-", 1)
                        seats_tuples.append((int(r_str), int(c_str)))
                    except Exception:
                        continue
                if seats_tuples:
                    release_seats_on_refund(show_id=int(show_id_for_cache), seats=seats_tuples)
            if remain_after_db is not None:
                redis_client.set(f"concert:show:{int(show_id_for_cache)}:remain:v1", int(remain_after_db))
    except Exception:
        pass

    return {"ok": True, "message": "환불이 완료되었습니다."}


@router.post("/api/write/user/bookings/refund")
def refund_booking(payload: Optional[Dict[str, Any]] = Body(default=None)):
    data = payload or {}
    user_id = _to_int(data.get("user_id"))
    booking_id = _to_int(data.get("booking_id"))
    booking_kind = (data.get("booking_kind") or "movie").strip().lower()

    if user_id <= 0 or booking_id <= 0:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": "요청값이 올바르지 않습니다."},
        )

    if booking_kind == "concert":
        return _refund_concert_booking(user_id, booking_id)
    return _refund_movie_booking(user_id, booking_id)
