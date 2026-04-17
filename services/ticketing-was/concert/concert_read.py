import json
from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from cache.redis_client import redis_client
from concert.concert_read_cache import (
    _fetch_confirmed_seat_keys_by_show,
    get_concert_bootstrap_cached_or_load,
    get_concert_bootstrap_for_show,
    get_concert_detail_cached_or_load,
    get_concerts_list_cached_or_load,
)
from concert.seat_hold import get_hold_revision, hold_count, hold_seats_snapshot
from db import get_db_read_connection
from config import CACHE_ENABLED

router = APIRouter()


@router.get("/api/read/concerts")
def list_concerts():
    return get_concerts_list_cached_or_load()


@router.get("/api/read/concert/{concert_id}")
def get_concert_detail(concert_id: int):
    payload = get_concert_detail_cached_or_load(concert_id)
    if not payload:
        return JSONResponse(status_code=404, content={"message": "not found"})
    return payload


@router.get("/api/read/concert/{concert_id}/booking-bootstrap")
def get_concert_booking_bootstrap(concert_id: int, show_id: Optional[int] = Query(default=None)):
    if show_id is not None and show_id > 0:
        payload = get_concert_bootstrap_for_show(concert_id, show_id)
    else:
        payload = get_concert_bootstrap_cached_or_load(concert_id)
    if not payload:
        return JSONResponse(status_code=404, content={"message": "not found"})
    return payload


@router.get("/api/read/concert/{concert_id}/booking-holds")
def get_concert_booking_holds(concert_id: int, show_id: int = Query(..., ge=1)):
    """
    DB 없이 Redis만: 처리중(주황) 좌석 + hold_rev(갱신 세대).
    - SQS 접수 시 write-api가 홀드 set에 넣는 순간(1차) / worker가 홀드를 풀 때(2차) rev가 올라간다.
    - 클라이언트는 rev가 변할 때만 좌석 DOM을 갱신하면 booking-bootstrap 전체보다 부하가 작다.
    """
    conn = get_db_read_connection()
    total_count: int = 0
    remain_count_db: int = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT total_count, remain_count FROM concert_shows "
                "WHERE concert_id = %s AND show_id = %s LIMIT 1",
                (int(concert_id), int(show_id)),
            )
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"ok": False, "code": "NOT_FOUND"})
            try:
                total_count = int(row.get("total_count") or 0)
            except Exception:
                total_count = 0
            try:
                remain_count_db = max(0, int(row.get("remain_count") or 0))
            except Exception:
                remain_count_db = 0
    finally:
        conn.close()

    sid = int(show_id)
    keys = hold_seats_snapshot(sid)

    def _sort_key(k: str) -> tuple[int, int]:
        parts = str(k or "").strip().split("-")
        if len(parts) != 2:
            return (0, 0)
        try:
            return (int(parts[0]), int(parts[1]))
        except Exception:
            return (0, 0)

    hold_sorted = sorted(keys, key=_sort_key)
    # Redis OFF(CACHE_ENABLED=false) 환경에서는 hold_rev 메타가 없으므로 0으로 둔다.
    rev = int(get_hold_revision(sid)) if CACHE_ENABLED else 0
    hc = int(hold_count(sid))

    # confirmed(회색)는 **DB ACTIVE 좌석**을 기준으로 한다.
    # (SQS/worker가 DB에 커밋한 뒤 confirmed set을 업데이트하므로, DB가 최종 근거)
    confirmed_from_db: list[str] = []
    try:
        confirmed_from_db = _fetch_confirmed_seat_keys_by_show([sid]).get(str(sid), []) or []
    except Exception:
        confirmed_from_db = []

    # remain_count:
    # - Redis ON: 운영 카운터(단일 진실) 사용
    # - Redis OFF: DB 기반으로 "확정 + 홀드"를 감안해 계산 (정확성 우선)
    remain_count_counter: Optional[int] = None
    if not CACHE_ENABLED:
        try:
            remain_count_counter = max(0, int(total_count) - int(len(confirmed_from_db)) - int(hc))
        except Exception:
            remain_count_counter = None
    else:
        try:
            raw = redis_client.get(f"concert:show:{sid}:remain:v1")
            if raw is None:
                if remain_count_db > 0:
                    try:
                        redis_client.setnx(f"concert:show:{sid}:remain:v1", int(remain_count_db))
                    except Exception:
                        pass
                remain_count_counter = int(remain_count_db)
            else:
                v = max(0, int(raw or 0))
                # 과거 버그로 remain key가 0으로 "굳은" 경우 복구:
                # hold/confirmed/pending이 모두 0이면 아직 판매/점유가 없다는 뜻이므로 DB seed로 복원해도 안전하다.
                if v <= 0 and remain_count_db > 0 and hc == 0 and len(confirmed_from_db) == 0:
                    try:
                        redis_client.set(f"concert:show:{sid}:remain:v1", int(remain_count_db))
                        v = int(remain_count_db)
                    except Exception:
                        pass
                remain_count_counter = v
        except Exception:
            remain_count_counter = None

    out: dict[str, Any] = {
        "ok": True,
        "concert_id": int(concert_id),
        "show_id": sid,
        "hold_seats": hold_sorted,
        "hold_count": hc,
        "hold_rev": rev,
        # 디버그/관측용(클라이언트는 무시 가능)
        "debug": {
            "total_count_db": int(total_count or 0),
            "remain_count_db": int(remain_count_db or 0),
            "confirmed_from_db_count": len(confirmed_from_db),
            "hold_count_snapshot": int(hc),
            "remain_count_counter": int(remain_count_counter) if remain_count_counter is not None else None,
        },
    }
    out["confirmed_seats"] = confirmed_from_db
    if remain_count_counter is not None:
        # 잔여는 total을 넘을 수 없다(캐시/리셋/드리프트 방어)
        if int(total_count) > 0:
            out["remain_count"] = min(int(remain_count_counter), int(total_count))
        else:
            out["remain_count"] = int(remain_count_counter)
    return out
