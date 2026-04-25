import json
from collections import defaultdict
from math import ceil

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cache.redis_client import redis_client
from db import get_db_read_connection
from movie.movie_cache_builder import (
    MOVIES_LIST_CACHE_KEY, _fetch_movies_from_db, _write_movies_cache,
)

router = APIRouter()

THEATERS_BOOTSTRAP_CACHE_KEY = "theaters:booking:bootstrap:v6"
THEATER_DETAIL_CACHE_KEY_FORMAT = "theaters:booking:detail:{theater_id}:v6"

EXCLUDED_BOOKING_STATUSES = {"CANCEL", "CANCELED", "CANCELLED", "EXPIRED", "FAILED"}


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _derive_region_name(address):
    text = str(address or "").strip()
    if not text:
        return "서울"
    if text.startswith("서울"):
        return "서울"
    if text.startswith("경기") or text.startswith("인천"):
        return "경기/인천"
    first = text.split()[0]
    if first in {"서울", "서울특별시"}:
        return "서울"
    if first in {"경기", "경기도", "인천", "인천광역시"}:
        return "경기/인천"
    return first


def _theaters_payload_from_rows(theater_rows):
    theaters = []
    for row in theater_rows:
        theater_id = _to_int(row.get("theater_id"))
        address = str(row.get("address") or "").strip()
        theaters.append({
            "theater_id": theater_id,
            "region_name": _derive_region_name(address),
            "theater_name": address or f"극장 {theater_id}",
            "address": address,
        })
    return theaters


def _build_special_tag(hall_name):
    value = str(hall_name or "").upper()
    if "ATMOS" in value:
        return "ATMOS"
    if "LASER" in value:
        return "LASER"
    if "IMAX" in value:
        return "IMAX"
    return "GENERAL"


def _get_theater_detail_cache_key(theater_id):
    return THEATER_DETAIL_CACHE_KEY_FORMAT.format(theater_id=theater_id)


def _is_excluded_from_booking(movie_row):
    title = str(movie_row.get("title") or "").strip()
    synopsis = str(movie_row.get("synopsis") or "").strip()
    if title.startswith("더미데이터") or synopsis.startswith("더미데이터"):
        return True
    genre = str(movie_row.get("genre") or "").strip()
    director = str(movie_row.get("director") or "").strip()
    if genre == "더미" and director == "더미":
        return True
    return False


def _load_movie_cache_rows():
    cached_data = None
    try:
        cached_data = redis_client.get(MOVIES_LIST_CACHE_KEY)
    except Exception:
        cached_data = None
    if cached_data:
        try:
            rows = json.loads(cached_data)
        except Exception:
            try:
                redis_client.delete(MOVIES_LIST_CACHE_KEY)
            except Exception:
                pass
            rows = []
    else:
        rows = _fetch_movies_from_db()
        _write_movies_cache(rows)
    movie_map = {}
    for row in rows:
        item = dict(row)
        if str(item.get("status") or "").upper() != "ACTIVE":
            continue
        if str(item.get("hide") or "N").upper() != "N":
            continue
        if _is_excluded_from_booking(item):
            continue
        movie_map[_to_int(item.get("movie_id"))] = item
    return movie_map


def _fetch_bootstrap_from_db(movie_map):
    """
    극장 예매 화면용 부트스트랩(웜업 대상 데이터).

    포함: 극장·홀·영화 메타(목록 캐시에서 온 공개 필드), 상영 스케줄, 잔여/총좌석,
         선점 좌석 좌표 \"row-col\" (booking_seats + hall_seats 조인, user_id 등 없음).
    미포함: 회원 프로필, 예매자 식별, 결제 식별자.
    """
    conn = get_db_read_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT theater_id, address FROM theaters ORDER BY theater_id ASC")
            theater_rows = cur.fetchall()

            cur.execute("""
                SELECT h.hall_id, h.theater_id, h.hall_name, h.total_seats,
                    MAX(hs.seat_row_no) AS seat_rows, MAX(hs.seat_col_no) AS seat_cols,
                    SUM(CASE WHEN UPPER(COALESCE(hs.status, 'ACTIVE')) = 'ACTIVE' THEN 1 ELSE 0 END) AS active_seat_count
                FROM halls h LEFT JOIN hall_seats hs ON hs.hall_id = h.hall_id
                GROUP BY h.hall_id, h.theater_id, h.hall_name, h.total_seats
                ORDER BY h.theater_id ASC, h.hall_id ASC
            """)
            hall_rows = cur.fetchall()

            cur.execute("""
                SELECT s.schedule_id, s.movie_id, s.hall_id, s.show_date, s.total_count,
                    -- remain_count는 단일 카운터(DB 컬럼)만 신뢰한다. (재계산/조인으로 만들지 않음)
                    GREATEST(0, COALESCE(s.remain_count, 0)) AS remain_count,
                    CASE
                      WHEN GREATEST(0, COALESCE(s.remain_count, 0)) <= 0 THEN 'CLOSED'
                      WHEN UPPER(COALESCE(s.status, '')) = 'CLOSED' THEN 'CLOSED'
                      ELSE 'OPEN'
                    END AS status
                FROM schedules s
                WHERE UPPER(COALESCE(s.status, 'OPEN')) IN ('OPEN', 'CLOSED')
                ORDER BY s.show_date ASC, s.schedule_id ASC
            """)
            schedule_rows = cur.fetchall()

            cur.execute("""
                SELECT bs.schedule_id, hs.seat_row_no, hs.seat_col_no, b.book_status
                FROM booking_seats bs
                JOIN hall_seats hs ON hs.seat_id = bs.seat_id
                JOIN booking b ON b.booking_id = bs.booking_id
                WHERE bs.status = 'ACTIVE'
                ORDER BY bs.schedule_id ASC, hs.seat_row_no ASC, hs.seat_col_no ASC
            """)
            reserved_rows = cur.fetchall()
    finally:
        conn.close()

    valid_schedule_ids = set()
    valid_hall_ids = set()
    valid_movie_ids = set()

    schedules = []
    for row in schedule_rows:
        movie_id = _to_int(row.get("movie_id"))
        hall_id = _to_int(row.get("hall_id"))
        if movie_id not in movie_map:
            continue
        schedule = dict(row)
        schedule["schedule_id"] = _to_int(schedule.get("schedule_id"))
        schedule["movie_id"] = movie_id
        schedule["hall_id"] = hall_id
        schedule["total_count"] = _to_int(schedule.get("total_count"))
        schedule["remain_count"] = _to_int(schedule.get("remain_count"))
        schedule["status"] = str(schedule.get("status") or "OPEN").upper()
        schedules.append(schedule)
        valid_schedule_ids.add(schedule["schedule_id"])
        valid_hall_ids.add(hall_id)
        valid_movie_ids.add(movie_id)

    halls = []
    for row in hall_rows:
        hall_id = _to_int(row.get("hall_id"))
        if hall_id not in valid_hall_ids:
            continue
        active_seat_count = _to_int(row.get("active_seat_count"))
        total_seats = _to_int(row.get("total_seats")) or active_seat_count or 30
        seat_rows = _to_int(row.get("seat_rows")) or 3
        seat_cols = _to_int(row.get("seat_cols")) or max(1, ceil(total_seats / max(seat_rows, 1)))
        halls.append({
            "hall_id": hall_id,
            "theater_id": _to_int(row.get("theater_id")),
            "hall_name": str(row.get("hall_name") or "A관").strip() or "A관",
            "total_seats": active_seat_count or total_seats,
            "seat_rows": seat_rows,
            "seat_cols": seat_cols,
            "special_tag": _build_special_tag(row.get("hall_name")),
        })

    theaters = _theaters_payload_from_rows(theater_rows)

    movies = []
    for movie_id in sorted(valid_movie_ids):
        if movie_id in movie_map:
            movies.append(movie_map[movie_id])

    reserved_seats = defaultdict(list)
    for row in reserved_rows:
        schedule_id = _to_int(row.get("schedule_id"))
        if schedule_id not in valid_schedule_ids:
            continue
        booking_status = str(row.get("book_status") or "").upper()
        if booking_status in EXCLUDED_BOOKING_STATUSES:
            continue
        seat_row_no = _to_int(row.get("seat_row_no"))
        seat_col_no = _to_int(row.get("seat_col_no"))
        if seat_row_no <= 0 or seat_col_no <= 0:
            continue
        reserved_seats[str(schedule_id)].append(f"{seat_row_no}-{seat_col_no}")

    return {
        "theaters": theaters,
        "halls": halls,
        "movies": movies,
        "schedules": schedules,
        "reservedSeats": dict(reserved_seats),
    }


def _write_bootstrap_cache(payload):
    redis_client.set(
        THEATERS_BOOTSTRAP_CACHE_KEY,
        json.dumps(payload, default=str, ensure_ascii=False),
    )


def _write_theater_detail_cache(theater_id, payload):
    redis_client.set(
        _get_theater_detail_cache_key(theater_id),
        json.dumps(payload, default=str, ensure_ascii=False),
    )


def _build_bootstrap_payload():
    movie_map = _load_movie_cache_rows()
    return _fetch_bootstrap_from_db(movie_map)


def refresh_theaters_bootstrap_cache():
    payload = _build_bootstrap_payload()
    _write_bootstrap_cache(payload)
    return {
        "cache_key": THEATERS_BOOTSTRAP_CACHE_KEY,
        "theater_count": len(payload.get("theaters") or []),
        "hall_count": len(payload.get("halls") or []),
        "movie_count": len(payload.get("movies") or []),
        "schedule_count": len(payload.get("schedules") or []),
    }


def _theater_detail_from_bootstrap(bootstrap: dict, theater_id: int):
    """단일 부트스트랩 dict에서 극장 상세 슬라이스 (DB 재조회 없음)."""
    theater = None
    for item in bootstrap.get("theaters") or []:
        if _to_int(item.get("theater_id")) == theater_id:
            theater = item
            break
    if theater is None:
        return None
    hall_ids = {
        _to_int(item.get("hall_id"))
        for item in bootstrap.get("halls") or []
        if _to_int(item.get("theater_id")) == theater_id
    }
    halls = [item for item in bootstrap.get("halls") or [] if _to_int(item.get("hall_id")) in hall_ids]
    schedules = [item for item in bootstrap.get("schedules") or [] if _to_int(item.get("hall_id")) in hall_ids]
    movie_ids = {_to_int(item.get("movie_id")) for item in schedules}
    movies = [item for item in bootstrap.get("movies") or [] if _to_int(item.get("movie_id")) in movie_ids]
    sched_ids = {_to_int(item.get("schedule_id")) for item in schedules}
    reserved_seats = {
        key: value
        for key, value in (bootstrap.get("reservedSeats") or {}).items()
        if _to_int(key) in sched_ids
    }
    return {"theater": theater, "halls": halls, "movies": movies, "schedules": schedules, "reservedSeats": reserved_seats}


def _bootstrap_and_theater_detail(theater_id: int):
    """한 번의 DB 스냅샷으로 부트스트랩 + 극장 상세. 캐시 미스 시 둘을 같이 써야 부트스트랩/상세 불일치가 없음."""
    bootstrap = _build_bootstrap_payload()
    return bootstrap, _theater_detail_from_bootstrap(bootstrap, theater_id)


def warmup_theaters_booking_caches():
    """
    웜업 전용: 영화 메타(이미 movies 캐시에서 로드)·극장·상영·잔여·선점 좌표만 Redis에 적재.
    극장별 상세 키(theaters:booking:detail:*:v6)는 동일 부트스트랩 1회로 채움 (불필요한 v1 키 없음).
    """
    bootstrap = _build_bootstrap_payload()
    _write_bootstrap_cache(bootstrap)
    n_detail = 0
    for t in bootstrap.get("theaters") or []:
        tid = _to_int(t.get("theater_id"))
        payload = _theater_detail_from_bootstrap(bootstrap, tid)
        if payload:
            _write_theater_detail_cache(tid, payload)
            n_detail += 1
    return {
        "name": "theaters_booking",
        "bootstrap_key": THEATERS_BOOTSTRAP_CACHE_KEY,
        "theater_detail_keys": n_detail,
        "schedule_count": len(bootstrap.get("schedules") or []),
        "reserved_schedule_keys": len(bootstrap.get("reservedSeats") or {}),
    }


@router.get("/api/read/theaters/bootstrap")
def get_theaters_bootstrap():
    cached_data = None
    try:
        cached_data = redis_client.get(THEATERS_BOOTSTRAP_CACHE_KEY)
    except Exception:
        cached_data = None
    if cached_data:
        try:
            return json.loads(cached_data)
        except Exception:
            try:
                redis_client.delete(THEATERS_BOOTSTRAP_CACHE_KEY)
            except Exception:
                pass
    payload = _build_bootstrap_payload()
    _write_bootstrap_cache(payload)
    return payload


@router.get("/api/read/theaters")
def get_theaters():
    payload = get_theaters_bootstrap()
    return payload.get("theaters") or []


@router.get("/api/read/theaters/remain-overrides")
def get_theaters_remain_overrides():
    return {}


@router.get("/api/read/theater/{theater_id}")
def get_theater_detail(theater_id: int):
    cache_key = _get_theater_detail_cache_key(theater_id)
    cached_data = None
    try:
        cached_data = redis_client.get(cache_key)
    except Exception:
        cached_data = None
    if cached_data:
        try:
            return json.loads(cached_data)
        except Exception:
            try:
                redis_client.delete(cache_key)
            except Exception:
                pass
    bootstrap, payload = _bootstrap_and_theater_detail(theater_id)
    if payload is None:
        return JSONResponse(status_code=404, content={"message": "not found"})
    _write_bootstrap_cache(bootstrap)
    _write_theater_detail_cache(theater_id, payload)
    return payload
