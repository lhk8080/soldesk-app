"""
Auth read endpoints (Cognito 전환 후).

- 로그인/비밀번호 검증은 Cognito가 처리하므로 제거.
- /api/read/auth/me: 미들웨어가 부착한 user_id로 프로필 조회.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from db import get_db_read_connection

router = APIRouter()


@router.get("/api/read/auth/me")
def auth_me(request: Request):
    """Cognito 인증 후 현재 사용자 정보 조회."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return JSONResponse(status_code=401, content={"message": "인증이 필요합니다."})
    conn = get_db_read_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, cognito_sub, email, name, phone FROM users WHERE user_id = %s",
                (int(user_id),),
            )
            user = cur.fetchone()
        if not user:
            return JSONResponse(status_code=404, content={"message": "사용자를 찾을 수 없습니다."})
        return {
            "message": "ok",
            "success": True,
            "user": {
                "user_id": user["user_id"],
                "email": user.get("email", ""),
                "name": user.get("name", ""),
                "phone": user.get("phone") or "",
            },
        }
    finally:
        conn.close()
