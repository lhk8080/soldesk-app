import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from urllib.error import URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cors_ensure_middleware import EnsureCrossOriginCredentialsMiddleware
from auth.auth_user_read import router as auth_user_read_router
from auth.cognito_middleware import CognitoAuthMiddleware
from concert.concert_read import router as concert_read_router
from concert.concert_read_cache import warmup_concert_caches
from movie.movie_cache_builder import rebuild_movie_cache
from movie.movie_read import router as movie_read_router
from theater.theaters_cache_builder import rebuild_theaters_cache
from theater.theaters_read import router as theaters_read_router
from user.user_read import router as user_read_router

log = logging.getLogger("read_app")

READ_ROUTERS = [
    user_read_router,
    movie_read_router,
    theaters_read_router,
    concert_read_router,
    auth_user_read_router,
]

READ_CACHE_TARGETS = [
    {
        "name": "movie_read",
        "router": movie_read_router,
        "refresher": rebuild_movie_cache,
    },
    {
        "name": "theaters_booking",
        "router": theaters_read_router,
        "refresher": rebuild_theaters_cache,
    },
    {
        "name": "concert_read",
        "router": concert_read_router,
        "refresher": warmup_concert_caches,
    },
]


def _warmup_concert_only_sync() -> None:
    """주기 웜업 경량 모드: 콘서트 목록(및 설정된 상세 웜업)만 갱신."""
    from config import CACHE_ENABLED

    if not CACHE_ENABLED:
        return
    try:
        result = warmup_concert_caches()
        log.info("cache warmup ok (concert only): %s", result)
    except Exception:
        log.exception("cache warmup failed (concert only)")


def _warmup_all_sync() -> None:
    """
    Redis(read 캐시) 웜업. 회원 PII 없음.

    콘서트는 CONCERT_CACHE_WARMUP_MODE=minimal 이면 공연 목록만 적재하고, 회차 스냅샷은
    요청 시 per-show + TTL로 채운다(대량 오픈 시 기동·메모리 부담 완화).

    각 refresher 내부 DB 조회는 get_db_read_connection().
    DB_READ_REPLICA_ENABLED=false(기본)이면 항상 writer. 리플리카 켠 뒤에는 리더 우선·실패 시 writer.
    웜업이 일부 실패해도 Pod는 뜨고, 요청 시 캐시 미스 → 같은 DB 폴백 경로로 화면이 살아난다.
    """
    from config import CACHE_ENABLED

    if not CACHE_ENABLED:
        return
    for target in READ_CACHE_TARGETS:
        name = target.get("name", "?")
        refresher = target.get("refresher")
        if not callable(refresher):
            continue
        try:
            result = refresher()
            log.info("cache warmup ok: %s %s", name, result)
        except Exception:
            log.exception("cache warmup failed: %s", name)


def _http_get_json(url: str, *, timeout_sec: float = 2.0) -> dict | None:
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=float(timeout_sec)) as resp:
            raw = resp.read()
        import json

        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _wait_for_write_startup_sync_sync() -> None:
    """
    read-api 기동 시점에, write-api startup sync(ok) 완료를 기다린다.
    실패/타임아웃이어도 read-api는 기동한다(과거 상태 웜업 가능성은 남지만 서비스 가용성이 우선).
    """
    from config import (
        READ_WAIT_FOR_WRITE_SYNC_INITIAL_BACKOFF_MS,
        READ_WAIT_FOR_WRITE_SYNC_MAX_BACKOFF_MS,
        READ_WAIT_FOR_WRITE_SYNC_MAX_SEC,
        WRITE_API_BASE_URL,
    )

    base = (WRITE_API_BASE_URL or "").rstrip("/")
    url = f"{base}/api/write/admin/startup-sync/status"
    deadline = time.monotonic() + max(0, int(READ_WAIT_FOR_WRITE_SYNC_MAX_SEC))

    backoff_ms = max(0, int(READ_WAIT_FOR_WRITE_SYNC_INITIAL_BACKOFF_MS))
    max_backoff_ms = max(0, int(READ_WAIT_FOR_WRITE_SYNC_MAX_BACKOFF_MS))

    while True:
        st = _http_get_json(url, timeout_sec=2.0) or {}
        status = str(st.get("status") or "")
        enabled = bool(st.get("enabled"))
        if not enabled or status == "skipped":
            log.info("write startup sync disabled/skip; proceeding warmup: %s", st)
            return
        if status == "ok":
            log.info("write startup sync ok; proceeding warmup: %s", st)
            return
        if time.monotonic() >= deadline:
            log.warning("write startup sync wait timeout; proceeding warmup: %s", st)
            return

        if backoff_ms <= 0:
            time.sleep(0.2)
        else:
            sleep_ms = int(backoff_ms * (0.8 + 0.4 * random.random()))
            time.sleep(max(0.0, float(sleep_ms) / 1000.0))
            backoff_ms = min(max_backoff_ms, max(backoff_ms, 1) * 2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from config import (
        CACHE_ENABLED,
        CACHE_WARMUP_ENABLED,
        CACHE_WARMUP_INTERVAL_SEC,
        CACHE_WARMUP_TOTAL_RUNS,
    )
    from config import READ_WAIT_FOR_WRITE_SYNC_ON_STARTUP

    repeat_task: asyncio.Task | None = None

    if CACHE_ENABLED and CACHE_WARMUP_ENABLED:
        from config import CACHE_WARMUP_REPEAT_LIGHT

        try:
            if READ_WAIT_FOR_WRITE_SYNC_ON_STARTUP:
                await asyncio.to_thread(_wait_for_write_startup_sync_sync)
            await asyncio.to_thread(_warmup_all_sync)
        except Exception:
            log.exception("initial cache warmup (thread) failed")

        extra = max(0, CACHE_WARMUP_TOTAL_RUNS - 1)
        if extra > 0 and CACHE_WARMUP_INTERVAL_SEC > 0:

            async def _repeat_warmup():
                for _ in range(extra):
                    await asyncio.sleep(CACHE_WARMUP_INTERVAL_SEC)
                    try:
                        if CACHE_WARMUP_REPEAT_LIGHT:
                            await asyncio.to_thread(_warmup_concert_only_sync)
                        else:
                            await asyncio.to_thread(_warmup_all_sync)
                    except Exception:
                        log.exception("scheduled cache warmup failed")

            repeat_task = asyncio.create_task(_repeat_warmup())

    yield

    if repeat_task is not None:
        repeat_task.cancel()
        try:
            await repeat_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Ticketing Read API", lifespan=lifespan)

# S3 웹사이트 등 다른 오리진에서 credentials 포함 요청 시 allow_origins=["*"] 는 브라우저 규격상 불가.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# CORSMiddleware가 붙이지 못하는 오류 응답에도 ACAO 보장 (브라우저 CORS 메시지 왜곡 방지)
app.add_middleware(EnsureCrossOriginCredentialsMiddleware)
# Cognito 인증 미들웨어: x-cognito-sub 헤더 → DB user_id 매핑
app.add_middleware(CognitoAuthMiddleware)

for router in READ_ROUTERS:
    app.include_router(router)


@app.get("/")
def root_health():
    return {"message": "ok"}


@app.get("/api/read/health")
def health():
    return {"message": "read api ok"}


@app.get("/api/read/version")
def version():
    return {
        "version": os.getenv("IMAGE_TAG", "unknown"),
        "env": os.getenv("ENV", "unknown"),
        "text": "시연 테스트용 -1",
    }
