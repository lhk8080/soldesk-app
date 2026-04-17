import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cors_ensure_middleware import EnsureCrossOriginCredentialsMiddleware
from auth.cognito_middleware import CognitoAuthMiddleware
from auth.auth_user_read import router as auth_user_read_router
from concert.concert_read import router as concert_read_router
from concert.concert_read_cache import warmup_concert_caches
from concert.concert_startup_reconcile import reconcile_concert_remain_on_startup
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    from config import (
        CACHE_ENABLED,
        CACHE_WARMUP_ENABLED,
        CACHE_WARMUP_INTERVAL_SEC,
        CACHE_WARMUP_TOTAL_RUNS,
    )

    repeat_task: asyncio.Task | None = None

    # 개발/재배포 시 DB 기준 remain 동기화(좌석 예매 건수 기반)
    try:
        await asyncio.to_thread(reconcile_concert_remain_on_startup)
    except Exception:
        log.exception("startup remain reconcile failed")

    if CACHE_ENABLED and CACHE_WARMUP_ENABLED:
        from config import CACHE_WARMUP_REPEAT_LIGHT

        try:
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
