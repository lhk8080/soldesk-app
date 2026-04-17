from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cors_ensure_middleware import EnsureCrossOriginCredentialsMiddleware
from auth.cognito_middleware import CognitoAuthMiddleware
from auth.auth_user_write import router as auth_user_write_router
from concert.concert_write import router as concert_write_router
from concert.concert_startup_reconcile import reconcile_concert_remain_on_startup
from cache.cache_builder import router as cache_builder_router
from theater.theaters_write import router as theaters_write_router
from user.user_write import router as user_write_router

WRITE_ROUTERS = [
    user_write_router,
    auth_user_write_router,
    theaters_write_router,
    concert_write_router,
    cache_builder_router,
]

app = FastAPI(title="Ticketing Write API")


@app.on_event("startup")
def _startup_reconcile_concert_remain():
    # 개발/재배포 시 DB 기준 remain 동기화(좌석 예매 건수 기반)
    try:
        reconcile_concert_remain_on_startup()
    except Exception:
        # write-api 기동 자체는 유지
        import logging

        logging.getLogger("write_app").exception("startup remain reconcile failed")

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
