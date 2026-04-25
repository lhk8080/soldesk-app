"""
Auth write endpoints (Cognito 전환 후).

- 회원가입/비밀번호 관리는 Cognito가 처리하므로 제거.
- 프로필 수정은 미들웨어의 request.state.user_id 기반으로 동작.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from db import get_db_connection

router = APIRouter()


@router.post("/api/write/auth/edit")
def auth_edit_user(request: Request, payload: Optional[Dict[str, Any]] = Body(default=None)):
    """사용자 프로필 수정 (이름만). Cognito 미들웨어가 부착한 user_id 사용.
    email 은 Cognito 가 소유하므로 이 엔드포인트로 변경 불가.
    phone 컬럼은 legacy 로 유지되지만 이 엔드포인트에서 더 이상 건드리지 않음."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return JSONResponse(status_code=401, content={"message": "인증이 필요합니다."})

    data = payload or {}
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"message": "이름을 입력해 주세요."})

    user_id_int = int(user_id)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id_int,))
            if not cur.fetchone():
                return JSONResponse(status_code=404, content={"message": "사용자를 찾을 수 없습니다."})
            cur.execute(
                "UPDATE users SET name = %s WHERE user_id = %s",
                (name, user_id_int),
            )
        conn.commit()
        return {"message": "edit success", "success": True}
    finally:
        conn.close()
