"""
Cognito + API Gateway 붙일 때 백엔드에서 쓸 스위치·설정 읽기.

현재: AUTH_MODE 기본 legacy → 이 모듈을 import해도 기존 요청 처리에 영향 없음.
이후: AUTH_MODE=jwt 일 때 미들웨어/Dependency에서 JWKS 검증 + sub → user 매핑 연결.
"""
from config import (
    AUTH_MODE,
    COGNITO_APP_CLIENT_ID,
    COGNITO_ISSUER,
    COGNITO_JWKS_URI,
)


def is_legacy_auth() -> bool:
    return AUTH_MODE != "jwt"


def cognito_issuer() -> str:
    return COGNITO_ISSUER


def cognito_jwks_uri() -> str:
    return COGNITO_JWKS_URI


def cognito_app_client_id() -> str:
    return COGNITO_APP_CLIENT_ID
