"""
Amazon ElastiCache — SQS 비동기 예매 상태 전용 연결 (논리 DB 분리).

조회 캐시(`redis_client`, ELASTICACHE_LOGICAL_DB_CACHE)와 키 공간을 나눠
동일 소형 노드 1대로 유지하면서 admin `FLUSHDB` 등이 booking:* 에 닿지 않게 한다.
"""
from typing import Any, Dict, Optional

from config import (
    BOOKING_STATE_ENABLED,
    ELASTICACHE_LOGICAL_DB_BOOKING,
    REDIS_CONNECT_TIMEOUT_SEC,
    REDIS_HEALTH_CHECK_INTERVAL_SEC,
    REDIS_HOST,
    REDIS_MAX_CONNECTIONS,
    REDIS_PORT,
    REDIS_SOCKET_TIMEOUT_SEC,
)


class _NoopBookingRedis:
    def get(self, key: str) -> Optional[str]:
        return None

    def setex(self, key: str, ttl_seconds: int, value: Any) -> bool:
        return True

    def delete(self, *keys: Any) -> int:
        return 0


if not BOOKING_STATE_ENABLED:
    elasticache_booking_client = _NoopBookingRedis()
else:
    import redis

    _pool_kw: Dict[str, Any] = {
        "host": REDIS_HOST,
        "port": REDIS_PORT,
        "db": int(ELASTICACHE_LOGICAL_DB_BOOKING),
        "decode_responses": True,
        "max_connections": REDIS_MAX_CONNECTIONS,
    }
    if REDIS_CONNECT_TIMEOUT_SEC > 0:
        _pool_kw["socket_connect_timeout"] = REDIS_CONNECT_TIMEOUT_SEC
    if REDIS_SOCKET_TIMEOUT_SEC > 0:
        _pool_kw["socket_timeout"] = REDIS_SOCKET_TIMEOUT_SEC
    if REDIS_HEALTH_CHECK_INTERVAL_SEC > 0:
        _pool_kw["health_check_interval"] = REDIS_HEALTH_CHECK_INTERVAL_SEC

    _pool = redis.ConnectionPool(**_pool_kw)
    elasticache_booking_client = redis.Redis(connection_pool=_pool)
