import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cors_ensure_middleware import EnsureCrossOriginCredentialsMiddleware
from auth.auth_user_write import router as auth_user_write_router
from auth.cognito_middleware import CognitoAuthMiddleware
from concert.concert_write import router as concert_write_router
from cache.cache_builder import router as cache_builder_router
from theater.theaters_write import router as theaters_write_router
from user.user_write import router as user_write_router

log = logging.getLogger("write_app")

_startup_sync_state: Dict[str, Any] = {
    "enabled": False,
    "status": "not_started",  # not_started | running | ok | failed | skipped
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}

WRITE_ROUTERS = [
    user_write_router,
    auth_user_write_router,
    theaters_write_router,
    concert_write_router,
    cache_builder_router,
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    from config import SYNC_REMAIN_COUNTS_ON_STARTUP

    if SYNC_REMAIN_COUNTS_ON_STARTUP:
        # write-api가 Ready 되기 전에(헬스체크 응답 전) 동기화를 끝내 웜업/조회가 과거 상태를 잡지 않게 한다.
        from db_sync import sync_remain_counts_and_refresh_redis

        _startup_sync_state["enabled"] = True
        _startup_sync_state["status"] = "running"
        _startup_sync_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _startup_sync_state["finished_at"] = None
        _startup_sync_state["result"] = None
        _startup_sync_state["error"] = None

        try:
            result = await asyncio.to_thread(sync_remain_counts_and_refresh_redis)
            _startup_sync_state["status"] = "ok"
            _startup_sync_state["result"] = result
            log.info("startup db sync ok: %s", result)
        except Exception:
            # 실패 시에도 원인 확인을 위해 로그를 남기되, CrashLoop가 더 큰 장애를 만들 수 있으므로 기동은 계속한다.
            _startup_sync_state["status"] = "failed"
            _startup_sync_state["error"] = "exception"
            log.exception("startup db sync failed")
        finally:
            _startup_sync_state["finished_at"] = datetime.now(timezone.utc).isoformat()
    else:
        _startup_sync_state["enabled"] = False
        _startup_sync_state["status"] = "skipped"

    yield


app = FastAPI(title="Ticketing Write API", lifespan=lifespan)


@app.get("/api/write/admin/startup-sync/status")
def startup_sync_status():
    return dict(_startup_sync_state)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(EnsureCrossOriginCredentialsMiddleware)
# Cognito 인증 미들웨어: x-cognito-sub 헤더 → DB user_id 매핑
app.add_middleware(CognitoAuthMiddleware)

for router in WRITE_ROUTERS:
    app.include_router(router)

@app.get("/")
def root_health():
    return {"message": "ok"}


@app.get("/api/write/health")
def health():
    return {"message": "write api ok"}
