"""
영화 read 캐시 (웜업 대상).

영화 메타 + 상영(schedules) 집계·잔여 등 공개 필드만 Redis에 적재. 회원·예매자 정보 없음.
DB는 get_db_read_connection() (기본 writer; DB_READ_REPLICA_ENABLED 시 리더 우선).
"""
import json
from datetime import date, datetime

from db import get_db_read_connection
from cache.redis_client import redis_client

MOVIES_LIST_CACHE_KEY = "movies:list:active_or_dummytitle:v5"
MOVIE_DETAIL_CACHE_KEY_FORMAT = "movie:detail:{movie_id}:v5"


def _to_date_value(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 10:
        text = text[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _format_release_date_display(value):
    parsed = _to_date_value(value)
    if not parsed:
        return "-"
    yy = str(parsed.year)[2:]
    mm = str(parsed.month).zfill(2)
    dd = str(parsed.day).zfill(2)
    return f"{yy}. {mm}. {dd}"


def _enrich_movie_row(movie):
    if not movie:
        return movie
    item = dict(movie)
    item["release_date_display"] = _format_release_date_display(item.get("release_date"))
    return item


def _get_movie_detail_cache_key(movie_id):
    return MOVIE_DETAIL_CACHE_KEY_FORMAT.format(movie_id=movie_id)


def _write_movies_cache(rows):
    redis_client.set(
        MOVIES_LIST_CACHE_KEY,
        json.dumps(rows, default=str, ensure_ascii=False)
    )


def _write_movie_detail_cache(movie_id, result):
    redis_client.set(
        _get_movie_detail_cache_key(movie_id),
        json.dumps(result, default=str, ensure_ascii=False)
    )


def _fetch_movies_from_db():
    conn = get_db_read_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    m.movie_id, m.title, m.genre, m.director, m.runtime_minutes,
                    m.poster_url, m.main_poster_url, m.video_url, m.audience_count,
                    m.release_date, m.synopsis, m.synopsis_line, m.status, m.hide,
                    MIN(CASE
                        WHEN UPPER(COALESCE(s.status, '')) = 'OPEN'
                             AND GREATEST(0, COALESCE(s.remain_count, 0)) > 0
                        THEN s.show_date END) AS next_show_date,
                    SUM(CASE
                        WHEN UPPER(COALESCE(s.status, '')) = 'OPEN'
                             AND GREATEST(0, COALESCE(s.remain_count, 0)) > 0
                        THEN GREATEST(0, COALESCE(s.remain_count, 0)) ELSE 0 END
                    ) AS total_remain_count
                FROM movies m
                LEFT JOIN schedules s ON m.movie_id = s.movie_id
                WHERE m.hide = 'N'
                  AND (m.status = 'ACTIVE' OR m.title LIKE '더미데이터%%' OR m.synopsis LIKE '더미데이터%%')
                GROUP BY m.movie_id, m.title, m.genre, m.director, m.runtime_minutes,
                    m.poster_url, m.main_poster_url, m.video_url, m.audience_count,
                    m.release_date, m.synopsis, m.synopsis_line, m.status, m.hide
                ORDER BY m.movie_id DESC
            """)
            rows = cur.fetchall()
        return [_enrich_movie_row(row) for row in rows]
    finally:
        conn.close()


def _fetch_movie_detail_from_db(movie_id):
    conn = get_db_read_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT movie_id, title, genre, director, runtime_minutes,
                    poster_url, main_poster_url, video_url, audience_count,
                    release_date, synopsis, synopsis_line, status, hide
                FROM movies
                WHERE movie_id = %s AND hide = 'N'
                  AND (status = 'ACTIVE' OR title LIKE '더미데이터%%' OR synopsis LIKE '더미데이터%%')
            """, (movie_id,))
            movie = cur.fetchone()
            if not movie:
                return None
            cur.execute("""
                SELECT s.schedule_id, s.show_date, s.total_count,
                    GREATEST(0, COALESCE(s.remain_count, 0)) AS remain_count,
                    CASE
                      WHEN GREATEST(0, COALESCE(s.remain_count, 0)) <= 0 THEN 'CLOSED'
                      WHEN UPPER(COALESCE(s.status, '')) = 'CLOSED' THEN 'CLOSED'
                      ELSE 'OPEN'
                    END AS status
                FROM schedules s
                WHERE s.movie_id = %s ORDER BY s.show_date ASC
            """, (movie_id,))
            schedules = cur.fetchall()
        movie = _enrich_movie_row(movie)
        return {"movie": movie, "schedules": schedules}
    finally:
        conn.close()


def refresh_movies_cache():
    rows = _fetch_movies_from_db()
    _write_movies_cache(rows)
    return {"cache_key": MOVIES_LIST_CACHE_KEY, "count": len(rows)}


def refresh_movie_detail_cache(movie_id):
    result = _fetch_movie_detail_from_db(movie_id)
    if result is None:
        redis_client.delete(_get_movie_detail_cache_key(movie_id))
        return {"movie_id": movie_id, "cached": False}
    _write_movie_detail_cache(movie_id, result)
    return {"movie_id": movie_id, "cached": True}


def rebuild_movie_cache():
    list_result = refresh_movies_cache()
    conn = get_db_read_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT movie_id FROM movies
                WHERE hide = 'N'
                  AND (status = 'ACTIVE' OR title LIKE '더미데이터%%' OR synopsis LIKE '더미데이터%%')
                ORDER BY movie_id ASC
            """)
            movie_rows = cur.fetchall()
    finally:
        conn.close()
    detail_cached_count = 0
    detail_deleted_count = 0
    for row in movie_rows:
        result = refresh_movie_detail_cache(row["movie_id"])
        if result["cached"]:
            detail_cached_count += 1
        else:
            detail_deleted_count += 1
    return {
        "name": "movie_read", "list": list_result,
        "detail_cached_count": detail_cached_count,
        "detail_deleted_count": detail_deleted_count,
    }
