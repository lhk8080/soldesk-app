import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cache.redis_client import redis_client
from movie.movie_cache_builder import (
    MOVIES_LIST_CACHE_KEY, _fetch_movie_detail_from_db, _fetch_movies_from_db,
    _get_movie_detail_cache_key, _write_movie_detail_cache, _write_movies_cache,
)

router = APIRouter()


def _get_movie_detail_payload(movie_id: int):
    cache_key = _get_movie_detail_cache_key(movie_id)
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
    result = _fetch_movie_detail_from_db(movie_id)
    if result is None:
        return None
    _write_movie_detail_cache(movie_id, result)
    return result


@router.get("/api/read/movies")
def get_movies():
    cached_data = None
    try:
        cached_data = redis_client.get(MOVIES_LIST_CACHE_KEY)
    except Exception:
        cached_data = None
    if cached_data:
        try:
            return json.loads(cached_data)
        except Exception:
            try:
                redis_client.delete(MOVIES_LIST_CACHE_KEY)
            except Exception:
                pass
    rows = _fetch_movies_from_db()
    _write_movies_cache(rows)
    return rows


@router.get("/api/read/movies/booking-bootstrap")
def get_movies_booking_bootstrap():
    from theater.theaters_read import get_theaters_bootstrap
    return get_theaters_bootstrap()


@router.get("/api/read/movies/detail/{movie_id}")
def get_movie_detail_under_list_prefix(movie_id: int):
    result = _get_movie_detail_payload(movie_id)
    if result is None:
        return JSONResponse(status_code=404, content={"message": "not found"})
    return result


@router.get("/api/read/movie/{movie_id}")
def get_movie_detail(movie_id: int):
    result = _get_movie_detail_payload(movie_id)
    if result is None:
        return JSONResponse(status_code=404, content={"message": "not found"})
    return result
