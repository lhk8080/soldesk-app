"""
Amazon ElastiCache for Redis — 조회(read) 캐시 전용 연결 (논리 DB = ELASTICACHE_LOGICAL_DB_CACHE).

SQS 비동기 예매 상태(booking:*)는 `cache.elasticache_booking_client` 의 별도 논리 DB를 쓴다.
동일 소형 노드 1대로 비용을 유지하면서 캐시 리빌드 FLUSHDB 가 예매 폴링 키를 지우지 않게 한다.
원본 데이터는 RDS이며, 채울 때는 get_db_read_connection() (DB_READ_REPLICA_ENABLED 시 리더 우선).
캐시 장애·미스 시 read 라우트는 DB로 폴백한다.

IMPORTANT:
- Do not delete this file. Other modules import `redis_client` from here.
- This module implements the cache "switch" policy:
  - CACHE_ENABLED=false: never touch Redis (no connect/timeout/retry) and behave as cache-miss/no-op.
  - CACHE_ENABLED=true : use real Redis client. If Redis is down, callers may hit retry/timeout in the redis library.
    (That behavior is intentional for the "enabled" mode.)
"""

from typing import Any, Dict, Optional

from config import (
    CACHE_ENABLED,
    ELASTICACHE_LOGICAL_DB_CACHE,
    REDIS_CONNECT_TIMEOUT_SEC,
    REDIS_HEALTH_CHECK_INTERVAL_SEC,
    REDIS_HOST,
    REDIS_MAX_CONNECTIONS,
    REDIS_PORT,
    REDIS_SOCKET_TIMEOUT_SEC,
)


class _NoopRedisClient:
  def get(self, key: str) -> Optional[str]:
    return None

  def mget(self, keys: Any, *args: Any, **kwargs: Any) -> list:
    try:
      n = len(keys)
    except TypeError:
      n = 0
    return [None] * n

  def set(self, key: str, value: Any, *args: Any, **kwargs: Any) -> bool:
    return True

  def setnx(self, key: str, value: Any) -> bool:
    return True

  def setex(self, key: str, ttl_seconds: int, value: Any) -> bool:
    return True

  def delete(self, *keys: Any) -> int:
    return 0

  def flushdb(self) -> bool:
    return True

  def pipeline(self, *args: Any, **kwargs: Any) -> Any:
    return self

  def execute(self, *args: Any, **kwargs: Any) -> list:
    return []

  def smembers(self, key: str) -> set:
    return set()

  def sadd(self, key: str, *values: Any) -> int:
    return 0

  def srem(self, key: str, *values: Any) -> int:
    return 0

  def scard(self, key: str) -> int:
    return 0

  def sismember(self, key: str, value: Any) -> bool:
    return False

  def incr(self, key: str) -> int:
    return 0

  def expire(self, key: str, ttl_seconds: int) -> bool:
    return True

  def expireat(self, key: str, when: Any) -> bool:
    return True

  def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
    return None

  def scan_iter(self, *args: Any, **kwargs: Any):
    # behave as empty iterator
    return iter(())


class _SafeRedisPipeline:
  """
  Redis가 다운/미설정인 환경에서도 read 경로가 죽지 않게:
  execute()에서 예외를 삼키고 빈 결과로 처리한다.
  """

  def __init__(self, inner: Any):
    self._inner = inner

  def __getattr__(self, name: str) -> Any:
    return getattr(self._inner, name)

  def execute(self, *args: Any, **kwargs: Any) -> list:
    try:
      res = self._inner.execute(*args, **kwargs)
    except Exception:
      return []
    return list(res or [])


class _SafeRedisClient:
  """
  ElastiCache(또는 Redis)가 없거나 장애일 때도 "캐시 미스"로 취급해
  호출자가 DB 조회로 폴백하도록 만든다.
  """

  def __init__(self, inner: Any):
    self._inner = inner

  def get(self, key: str) -> Optional[str]:
    try:
      return self._inner.get(key)
    except Exception:
      return None

  def mget(self, keys: Any, *args: Any, **kwargs: Any) -> list:
    try:
      res = self._inner.mget(keys, *args, **kwargs)
      return list(res or [])
    except Exception:
      try:
        n = len(keys)
      except TypeError:
        n = 0
      return [None] * n

  def set(self, key: str, value: Any, *args: Any, **kwargs: Any) -> bool:
    try:
      return bool(self._inner.set(key, value, *args, **kwargs))
    except Exception:
      return True

  def setnx(self, key: str, value: Any) -> bool:
    """
    Redis SETNX 호환.
    redis-py에서는 set(name, value, nx=True)로 구현되므로 그 형태로 위임한다.
    """
    try:
      return bool(self._inner.set(key, value, nx=True))
    except Exception:
      return True

  def setex(self, key: str, ttl_seconds: int, value: Any) -> bool:
    try:
      return bool(self._inner.setex(key, int(ttl_seconds), value))
    except Exception:
      return True

  def delete(self, *keys: Any) -> int:
    try:
      return int(self._inner.delete(*keys) or 0)
    except Exception:
      return 0

  def flushdb(self) -> bool:
    try:
      return bool(self._inner.flushdb())
    except Exception:
      return True

  def pipeline(self, *args: Any, **kwargs: Any) -> Any:
    try:
      return _SafeRedisPipeline(self._inner.pipeline(*args, **kwargs))
    except Exception:
      return _NoopRedisClient()

  def smembers(self, key: str) -> set:
    try:
      res = self._inner.smembers(key) or set()
      return set(res)
    except Exception:
      return set()

  def sadd(self, key: str, *values: Any) -> int:
    try:
      return int(self._inner.sadd(key, *values) or 0)
    except Exception:
      return 0

  def srem(self, key: str, *values: Any) -> int:
    try:
      return int(self._inner.srem(key, *values) or 0)
    except Exception:
      return 0

  def scard(self, key: str) -> int:
    try:
      return int(self._inner.scard(key) or 0)
    except Exception:
      return 0

  def sismember(self, key: str, value: Any) -> bool:
    try:
      return bool(self._inner.sismember(key, value))
    except Exception:
      return False

  def incr(self, key: str) -> int:
    try:
      return int(self._inner.incr(key) or 0)
    except Exception:
      return 0

  def expire(self, key: str, ttl_seconds: int) -> bool:
    try:
      return bool(self._inner.expire(key, int(ttl_seconds)))
    except Exception:
      return True

  def expireat(self, key: str, when: Any) -> bool:
    try:
      return bool(self._inner.expireat(key, when))
    except Exception:
      return True

  def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
    try:
      return self._inner.eval(script, int(numkeys), *keys_and_args)
    except Exception:
      return None

  def scan_iter(self, *args: Any, **kwargs: Any):
    """
    redis-py scan_iter passthrough.
    reset/maintenance code relies on this to delete patterned keys safely.
    """
    try:
      return self._inner.scan_iter(*args, **kwargs)
    except Exception:
      return iter(())

if not CACHE_ENABLED:
  # hard bypass: do NOT create a Redis client at all.
  redis_client = _NoopRedisClient()
else:
  import redis  # lazy import so CACHE_ENABLED=false doesn't load/initialize redis at all

  _pool_kw: Dict[str, Any] = {
      "host": REDIS_HOST,
      "port": REDIS_PORT,
      "db": int(ELASTICACHE_LOGICAL_DB_CACHE),
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
  redis_client = _SafeRedisClient(redis.Redis(connection_pool=_pool))
