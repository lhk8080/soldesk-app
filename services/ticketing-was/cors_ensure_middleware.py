"""
500/비정상 응답에서도 브라우저가 본문을 읽을 수 있도록 ACAO를 보강한다.
(S3 웹사이트 오리진 → ALB API 크로스 오리진 + credentials)
"""
from starlette.middleware.base import BaseHTTPMiddleware


class EnsureCrossOriginCredentialsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        origin = request.headers.get("origin")
        if not origin or not origin.startswith(("http://", "https://")):
            return response
        if response.headers.get("access-control-allow-origin"):
            return response
        response.headers["access-control-allow-origin"] = origin
        response.headers["access-control-allow-credentials"] = "true"
        return response
