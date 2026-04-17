from threading import Lock

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cache.redis_client import redis_client
from read_app import READ_CACHE_TARGETS

router = APIRouter()

_cache_rebuild_lock = Lock()


@router.post("/api/write/admin/cache/rebuild-all")
def rebuild_all_cache():
    if not _cache_rebuild_lock.acquire(blocking=False):
        return JSONResponse(
            status_code=409,
            content={"message": "cache rebuild already running", "success": False},
        )
    try:
        redis_client.flushdb()
        results = []
        for target in READ_CACHE_TARGETS:
            refresher = target.get("refresher")
            if not callable(refresher):
                continue
            result = refresher()
            results.append({
                "name": target.get("name"),
                "router": getattr(target.get("router"), "prefix", ""),
                "result": result,
            })
        return {"message": "cache rebuild success", "success": True, "results": results}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"message": f"cache rebuild failed: {str(e)}", "success": False},
        )
    finally:
        _cache_rebuild_lock.release()


@router.post("/api/read/movies/cache/rebuild-all")
def rebuild_all_cache_read_alias():
    return rebuild_all_cache()
