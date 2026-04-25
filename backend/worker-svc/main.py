"""
SQS FIFO Worker — 예매 메시지 처리.
theaters_write._commit_booking_sync / concert_write._commit_concert_booking_sync 로직을 재사용.

중요(콘서트):
- 기존에는 concert_shows 1행을 `FOR UPDATE`로 잠가 동일 회차(show_id)를 사실상 직렬 처리했다.
- 콘서트 티켓팅의 동시성 단위는 "회차 전체"가 아니라 "좌석"이므로,
  `concert_booking_seats`의 ACTIVE 유니크 인덱스(show_id,row,col)를 락 단위로 사용한다.
  → 회차는 1개여도 서로 다른 좌석은 병렬로 처리 가능(파드/스레드 확장 효과가 보임).
"""
import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import redis
import pymysql
import time
import threading
import random
from botocore.config import Config

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker-svc")

AWS_REGION    = os.getenv("AWS_REGION", "")
SQS_QUEUE_NAME = (os.getenv("SQS_QUEUE_NAME") or "ticketing-reservation.fifo").strip()
DB_HOST       = os.getenv("DB_WRITER_HOST", "127.0.0.1")
DB_PORT       = int(os.getenv("DB_PORT", "3306"))
DB_NAME       = os.getenv("DB_NAME", "ticketing")
DB_USER       = os.getenv("DB_USER", "root")
DB_PASSWORD   = os.getenv("DB_PASSWORD", "")
ELASTICACHE_PRIMARY_ENDPOINT = os.getenv("ELASTICACHE_PRIMARY_ENDPOINT", "").strip()
REDIS_HOST    = ELASTICACHE_PRIMARY_ENDPOINT or os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT    = int(os.getenv("REDIS_PORT", os.getenv("ELASTICACHE_PORT", "6379")))


def _get_int(name, default, minimum=0):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return max(minimum, default)
    try:
        return max(minimum, int(str(raw).strip(), 10))
    except ValueError:
        return max(minimum, default)


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off"):
        return False
    return default


# ── Demo: optional artificial delay (OFF by default) ──────────────────────────
# 목적: 시연에서 큐(backlog)를 일부러 만들어 "처리중(주황)" 연출을 보기 쉽게 함.
# 원칙: 기본은 OFF. 환경변수로만 ON.
WORKER_DEMO_DELAY_ENABLED = _get_bool("WORKER_DEMO_DELAY_ENABLED", False)
WORKER_DEMO_DELAY_MS = _get_int("WORKER_DEMO_DELAY_MS", 0, 0)
WORKER_DEMO_DELAY_JITTER_MS = _get_int("WORKER_DEMO_DELAY_JITTER_MS", 0, 0)


def _maybe_sleep_demo_delay() -> None:
    if not WORKER_DEMO_DELAY_ENABLED:
        return
    base = int(WORKER_DEMO_DELAY_MS)
    jitter = int(WORKER_DEMO_DELAY_JITTER_MS)
    if base <= 0 and jitter <= 0:
        return
    extra = random.randint(0, max(0, jitter)) if jitter > 0 else 0
    ms = max(0, base + extra)
    if ms > 0:
        time.sleep(ms / 1000.0)


# ticketing-was/config.py 와 동일 규칙: SQS 큐 이름이 있으면 예매 상태 ElastiCache 필수 → 자동 True
BOOKING_STATE_ENABLED = _get_bool("BOOKING_STATE_ENABLED", True)
if SQS_QUEUE_NAME:
    BOOKING_STATE_ENABLED = True

# Redis remain → DB remain 비동기 동기화(옵션)
REMAIN_DB_SYNC_ENABLED = _get_bool("REMAIN_DB_SYNC_ENABLED", False)
REMAIN_DB_SYNC_INTERVAL_SEC = _get_int("REMAIN_DB_SYNC_INTERVAL_SEC", 2, 0)
REMAIN_DB_SYNC_BATCH_SIZE = _get_int("REMAIN_DB_SYNC_BATCH_SIZE", 200, 1)
REMAIN_DB_SYNC_LOCK_TTL_SEC = _get_int("REMAIN_DB_SYNC_LOCK_TTL_SEC", 3, 1)

# read-api 와 동일: 조회 캐시 무효화만 noop (연결 생략)
CACHE_ENABLED = _get_bool("CACHE_ENABLED", True)

REDIS_MAX_CONNECTIONS = _get_int("REDIS_MAX_CONNECTIONS", 50, 1)
REDIS_SOCKET_TIMEOUT_SEC = _get_int("REDIS_SOCKET_TIMEOUT_SEC", 5, 0)
REDIS_CONNECT_TIMEOUT_SEC = _get_int("REDIS_CONNECT_TIMEOUT_SEC", 3, 0)
REDIS_HEALTH_CHECK_INTERVAL_SEC = _get_int("REDIS_HEALTH_CHECK_INTERVAL_SEC", 30, 0)

# ticketing-was/theater/theaters_read.py 와 동일해야 함
THEATERS_BOOTSTRAP_CACHE_KEY = "theaters:booking:bootstrap:v6"

CONCERT_REMAIN_DIRTY_SET_KEY = "concert:remain_dirty:show_ids:v1"


def _theaters_detail_cache_key(theater_id: int) -> str:
    return f"theaters:booking:detail:{int(theater_id)}:v6"

_pool_kw = {
    "host": REDIS_HOST,
    "port": REDIS_PORT,
    "decode_responses": True,
    "max_connections": REDIS_MAX_CONNECTIONS,
}
if REDIS_CONNECT_TIMEOUT_SEC > 0:
    _pool_kw["socket_connect_timeout"] = REDIS_CONNECT_TIMEOUT_SEC
if REDIS_SOCKET_TIMEOUT_SEC > 0:
    _pool_kw["socket_timeout"] = REDIS_SOCKET_TIMEOUT_SEC
if REDIS_HEALTH_CHECK_INTERVAL_SEC > 0:
    _pool_kw["health_check_interval"] = REDIS_HEALTH_CHECK_INTERVAL_SEC

ELASTICACHE_LOGICAL_DB_CACHE = min(15, _get_int("ELASTICACHE_LOGICAL_DB_CACHE", 0, 0))
ELASTICACHE_LOGICAL_DB_BOOKING = min(15, _get_int("ELASTICACHE_LOGICAL_DB_BOOKING", 1, 0))
if ELASTICACHE_LOGICAL_DB_BOOKING == ELASTICACHE_LOGICAL_DB_CACHE:
    ELASTICACHE_LOGICAL_DB_BOOKING = (ELASTICACHE_LOGICAL_DB_CACHE + 1) % 16

_cache_pool_kw = {**_pool_kw, "db": ELASTICACHE_LOGICAL_DB_CACHE}
_booking_pool_kw = {**_pool_kw, "db": ELASTICACHE_LOGICAL_DB_BOOKING}

# booking:result TTL(초) — 큐 적체/지연이 길 때도 결과 조회가 가능하도록 기본을 넉넉히 둔다.
BOOKING_RESULT_TTL_SEC = _get_int("BOOKING_RESULT_TTL_SEC", 3600, 60)
# booking queue(연출용) 카운터 TTL(초). write-api BOOKING_QUEUE_COUNTER_TTL_SEC 와 동일 값 권장.
BOOKING_QUEUE_COUNTER_TTL_SEC = _get_int("BOOKING_QUEUE_COUNTER_TTL_SEC", 7200, 60)
# 콘서트 확정(회색) 좌석 Redis set TTL 정책:
# - 기본: 공연 종료(show_date) + grace 초에 EXPIREAT
# - show_date를 못 얻으면 fallback TTL(초) 사용 (0이면 만료 없음)
CONCERT_CONFIRMED_SET_FALLBACK_TTL_SEC = _get_int("CONCERT_CONFIRMED_SET_TTL_SEC", 0, 0)
CONCERT_CONFIRMED_SET_GRACE_SEC = _get_int("CONCERT_CONFIRMED_SET_GRACE_SEC", 6 * 3600, 0)


class _NoopBookingRedis:
    def get(self, key):
        return None

    def setex(self, key, ttl_seconds, value):
        return True

    def delete(self, *keys):
        return 0


class _NoopCacheRedis:
    def delete(self, *keys):
        return 0


if CACHE_ENABLED:
    elasticache_read_cache_client = redis.Redis(
        connection_pool=redis.ConnectionPool(**_cache_pool_kw)
    )
else:
    elasticache_read_cache_client = _NoopCacheRedis()

if BOOKING_STATE_ENABLED:
    elasticache_booking_client = redis.Redis(
        connection_pool=redis.ConnectionPool(**_booking_pool_kw)
    )
else:
    elasticache_booking_client = _NoopBookingRedis()

SQS_RECEIVE_MAX_MESSAGES = min(10, max(1, _get_int("SQS_RECEIVE_MAX_MESSAGES", 10, 1)))
# 한 번 Receive로 온 메시지(서로 다른 FIFO 그룹)를 동시에 처리. 1이면 기존과 같이 순차.
# (데모/부하 환경에서는 20~50까지도 유효. 단, DB 동시성 제한(WORKER_DB_MAX_CONCURRENT)과 함께 올릴 것.)
WORKER_SQS_BATCH_CONCURRENCY = min(50, max(1, _get_int("WORKER_SQS_BATCH_CONCURRENCY", 20, 1)))
# 폴링 루프 스레드 수(파드당). FIFO 샤딩 + 다수 poller 조합이 burst 처리량을 크게 끌어올린다.
WORKER_SQS_POLLERS = min(10, max(1, _get_int("WORKER_SQS_POLLERS", 2, 1)))
SQS_WAIT_TIME_SECONDS = min(20, max(0, _get_int("SQS_WAIT_TIME_SECONDS", 20, 0)))
SQS_POLL_ERROR_BACKOFF_SEC = max(1, _get_int("SQS_POLL_ERROR_BACKOFF_SEC", 3, 1))
SQS_BOTO_MAX_ATTEMPTS = _get_int("SQS_BOTO_MAX_ATTEMPTS", 5, 1)
_sqs_retry_mode = os.getenv("SQS_BOTO_RETRY_MODE", "adaptive").strip().lower()
if _sqs_retry_mode not in ("standard", "adaptive"):
    _sqs_retry_mode = "standard"
_sqs_connect = max(1, _get_int("SQS_CONNECT_TIMEOUT_SEC", 5, 1))
_sqs_read = max(1, _get_int("SQS_READ_TIMEOUT_SEC", 30, 1))

_sqs_config = Config(
    retries={"max_attempts": SQS_BOTO_MAX_ATTEMPTS, "mode": _sqs_retry_mode},
    connect_timeout=_sqs_connect,
    read_timeout=_sqs_read,
)
sqs = boto3.client("sqs", region_name=AWS_REGION, config=_sqs_config)


def _resolve_queue_url() -> str:
    qname = str(SQS_QUEUE_NAME or "").strip()
    if not qname:
        raise RuntimeError("SQS_QUEUE_NAME is required")
    resp = sqs.get_queue_url(QueueName=qname)
    url = str(resp.get("QueueUrl") or "").strip()
    if not url:
        raise RuntimeError("GetQueueUrl returned empty QueueUrl")
    return url


SQS_QUEUE_URL = _resolve_queue_url()

# 파드당 DB 동시 실행 제한(과도한 커넥션/락 경합으로 전체 처리량이 떨어지는 것을 방지).
# 값이 크면 DB 부하가 커지고, KEDA 데모에서는 처리량 증가가 더 선명해진다(단 RDS 한도 주의).
_db_max_conc_default = max(1, min(200, WORKER_SQS_BATCH_CONCURRENCY * WORKER_SQS_POLLERS))
WORKER_DB_MAX_CONCURRENT = max(
    1,
    min(
        200,
        _get_int("WORKER_DB_MAX_CONCURRENT", _db_max_conc_default, 1),
    ),
)
_db_sem = threading.Semaphore(WORKER_DB_MAX_CONCURRENT)

# 배치 처리 스레드풀은 재사용(배치마다 생성/파괴하지 않음).
_batch_pool = ThreadPoolExecutor(max_workers=WORKER_SQS_BATCH_CONCURRENCY)


class _Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.received = 0
        self.acked = 0
        self.process_ok = 0
        self.process_fail = 0
        self.db_sem_wait_ms_sum = 0.0
        self.db_sem_wait_ms_max = 0.0
        self.handle_ms_sum = 0.0
        self.handle_ms_max = 0.0
        self.last_error = ""

    def inc_received(self, n: int):
        with self._lock:
            self.received += int(n)

    def inc_acked(self):
        with self._lock:
            self.acked += 1

    def record_ok(self, handle_ms: float, sem_wait_ms: float):
        with self._lock:
            self.process_ok += 1
            self.handle_ms_sum += handle_ms
            self.handle_ms_max = max(self.handle_ms_max, handle_ms)
            self.db_sem_wait_ms_sum += sem_wait_ms
            self.db_sem_wait_ms_max = max(self.db_sem_wait_ms_max, sem_wait_ms)

    def record_fail(self, handle_ms: float, sem_wait_ms: float, err: str):
        with self._lock:
            self.process_fail += 1
            self.handle_ms_sum += handle_ms
            self.handle_ms_max = max(self.handle_ms_max, handle_ms)
            self.db_sem_wait_ms_sum += sem_wait_ms
            self.db_sem_wait_ms_max = max(self.db_sem_wait_ms_max, sem_wait_ms)
            self.last_error = (err or "")[:500]

    def snapshot(self) -> dict:
        with self._lock:
            total = self.process_ok + self.process_fail
            avg_handle = (self.handle_ms_sum / total) if total else 0.0
            avg_sem = (self.db_sem_wait_ms_sum / total) if total else 0.0
            return {
                "started_at": self.started_at,
                "uptime_sec": max(0.0, time.time() - self.started_at),
                "sqs": {
                    "received": self.received,
                    "acked": self.acked,
                    "inflight_est": max(0, self.received - self.acked),
                },
                "process": {
                    "ok": self.process_ok,
                    "fail": self.process_fail,
                    "avg_handle_ms": round(avg_handle, 3),
                    "max_handle_ms": round(self.handle_ms_max, 3),
                    "avg_db_sem_wait_ms": round(avg_sem, 3),
                    "max_db_sem_wait_ms": round(self.db_sem_wait_ms_max, 3),
                    "last_error": self.last_error,
                },
                "config": {
                    "max_messages": SQS_RECEIVE_MAX_MESSAGES,
                    "batch_concurrency": WORKER_SQS_BATCH_CONCURRENCY,
                    "pollers": WORKER_SQS_POLLERS,
                    "db_max_concurrent": WORKER_DB_MAX_CONCURRENT,
                    "wait_time_sec": SQS_WAIT_TIME_SECONDS,
                },
            }


_stats = _Stats()


def get_tx_conn():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _sync_one_concert_show_remain_from_redis(show_id: int) -> bool:
    """
    Redis remain 카운터(concert:show:{id}:remain:v1)를 DB concert_shows.remain_count로 best-effort 반영.
    - DB 값이 이미 Redis와 같으면 UPDATE를 스킵(=조회만 하고 끝).
    Returns: True if updated, False if skipped/failed.
    """
    sid = _to_int(show_id)
    if sid <= 0:
        return False
    try:
        raw = elasticache_read_cache_client.get(f"concert:show:{sid}:remain:v1")
        if raw is None:
            return False
        redis_remain = max(0, _to_int(raw, 0))
    except Exception:
        return False

    conn = get_tx_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT remain_count, total_count FROM concert_shows WHERE show_id=%s LIMIT 1",
                (sid,),
            )
            row = cur.fetchone()
            if not row:
                return False
            db_remain = max(0, _to_int(row.get("remain_count"), 0))
            total = max(0, _to_int(row.get("total_count"), 0))
            # total_count 방어
            if total > 0:
                redis_remain = min(redis_remain, total)
            if db_remain == redis_remain:
                return False
            status = "CLOSED" if redis_remain <= 0 else "OPEN"
            cur.execute(
                "UPDATE concert_shows SET remain_count=%s, status=%s WHERE show_id=%s",
                (redis_remain, status, sid),
            )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _remain_db_sync_loop() -> None:
    if not REMAIN_DB_SYNC_ENABLED:
        return
    if not CACHE_ENABLED:
        # Redis를 안 쓰는 모드면 동기화할 remain 자체가 없으니 스킵
        return
    interval = max(0, int(REMAIN_DB_SYNC_INTERVAL_SEC))
    batch = max(1, int(REMAIN_DB_SYNC_BATCH_SIZE))
    lock_ttl = max(1, int(REMAIN_DB_SYNC_LOCK_TTL_SEC))
    lock_key = "concert:remain_db_sync:lock:v1"

    while True:
        try:
            # 여러 파드가 있으면 중복 실행을 막기 위해 Redis lock(SET NX EX)을 사용
            try:
                if not elasticache_read_cache_client.set(lock_key, "1", nx=True, ex=lock_ttl):
                    time.sleep(0.2 if interval <= 0 else min(1.0, interval))
                    continue
            except Exception:
                # lock 실패면 보수적으로 sleep
                time.sleep(0.5)
                continue

            show_ids: list[int] = []
            try:
                # spop은 set에서 원소를 제거하며 가져옴(중복 처리 방지)
                for _ in range(batch):
                    v = elasticache_read_cache_client.spop(CONCERT_REMAIN_DIRTY_SET_KEY)
                    if v is None:
                        break
                    sid = _to_int(v, 0)
                    if sid > 0:
                        show_ids.append(sid)
            except Exception:
                show_ids = []

            if show_ids:
                updated = 0
                skipped = 0
                for sid in show_ids:
                    if _sync_one_concert_show_remain_from_redis(sid):
                        updated += 1
                    else:
                        skipped += 1
                log.info(
                    "remain_db_sync batch done: shows=%s updated=%s skipped=%s",
                    len(show_ids),
                    updated,
                    skipped,
                )

        except Exception:
            log.exception("remain_db_sync loop error")

        if interval <= 0:
            time.sleep(0.2)
        else:
            time.sleep(float(interval))

def _parse_seat_key(value):
    parts = str(value or "").strip().split("-")
    if len(parts) != 2:
        return None
    r, c = _to_int(parts[0]), _to_int(parts[1])
    return (r, c) if r > 0 and c > 0 else None


def _is_mysql_deadlock(exc: Exception) -> bool:
    """
    MySQL/InnoDB deadlock: OperationalError 1213
    """
    try:
        code = int(getattr(exc, "args", [None])[0])
        return code == 1213
    except Exception:
        return False


def _maybe_close_concert_show_if_soldout(show_id: int, remain_count_after: int) -> None:
    """
    concert_shows 1행은 모든 요청이 공유하는 핫스팟이라, 매 예매마다 UPDATE 하면 데드락/락 경합이 급증한다.
    remain_count_after <= 0 일 때만 'CLOSED'를 best-effort로 반영한다. (OPEN 복구는 하지 않음)
    """
    if int(show_id) <= 0:
        return
    if _to_int(remain_count_after, 0) > 0:
        return
    tries = _get_int("WORKER_DB_DEADLOCK_RETRIES", 5, 1)
    for attempt in range(1, tries + 1):
        conn = get_tx_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE concert_shows SET status = 'CLOSED' "
                    "WHERE show_id = %s AND COALESCE(status,'') <> 'CLOSED'",
                    (int(show_id),),
                )
            conn.commit()
            return
        except pymysql.err.OperationalError as oe:
            conn.rollback()
            if _is_mysql_deadlock(oe) and attempt < tries:
                time.sleep(min(0.5, 0.02 * (2 ** (attempt - 1))) + random.uniform(0, 0.025))
                continue
            log.warning("concert_shows CLOSED 반영 실패(무시) show_id=%s: %s", show_id, oe)
            return
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.warning("concert_shows CLOSED 반영 실패(무시) show_id=%s: %s", show_id, e)
            return
        finally:
            conn.close()


def _generate_booking_code():
    import secrets, string
    letters = "".join(secrets.choice(string.ascii_uppercase) for _ in range(2))
    digits = "".join(secrets.choice(string.digits) for _ in range(6))
    return f"{letters}{digits}"


def _booking_result_key(booking_ref: str) -> str:
    return f"booking:result:{booking_ref}"


def _booking_queued_key(booking_ref: str) -> str:
    """write-api sqs_client._booking_queued_key 과 동일한 규약."""
    return f"booking:queued:{booking_ref}"


def _queue_done_counter_key(booking_type: str, entity_id: int) -> str:
    return f"booking:queue:{str(booking_type)}:{int(entity_id)}:done"


def store_result(booking_ref, result, *, booking_type: str | None = None, entity_id: int | None = None):
    """ElastiCache booking 논리 DB에 결과 저장(write-api 폴링). 완료 시 queued 제거."""
    key = _booking_result_key(booking_ref)
    elasticache_booking_client.setex(key, int(BOOKING_RESULT_TTL_SEC), json.dumps(result, default=str))
    try:
        elasticache_booking_client.delete(_booking_queued_key(booking_ref))
    except Exception:
        log.debug("booking queued 키 삭제 실패(무시)", exc_info=True)
    if booking_type and entity_id and int(entity_id) > 0:
        try:
            k = _queue_done_counter_key(str(booking_type), int(entity_id))
            elasticache_booking_client.incr(k)
            # 연출용 카운터는 오픈/런 단위로 자연 만료(누적 방지)
            try:
                ttl = int(BOOKING_QUEUE_COUNTER_TTL_SEC)
                if ttl > 0:
                    elasticache_booking_client.expire(k, ttl)
            except Exception:
                pass
        except Exception:
            log.debug("queue done counter incr 실패(무시) type=%s entity_id=%s", booking_type, entity_id, exc_info=True)
    log.info("결과 저장: %s", key)


def store_failure_result(booking_ref, result, *, booking_type: str | None = None, entity_id: int | None = None):
    """
    실패 결과를 기록하되, 이미 OK로 확정된 폴링 결과는 덮어쓰지 않는다.
    SQS 재전달·병렬 처리 레이스로 첫 처리는 DB 커밋까지 끝났는데 두 번째가 좌석 유니크에 걸려
    DUPLICATE_SEAT를 쓰면 클라이언트가 '먼저 성공한' 예매를 실패로 보게 된다.
    """
    key = _booking_result_key(booking_ref)
    try:
        raw = elasticache_booking_client.get(key)
        if raw:
            prev = json.loads(raw)
            if prev.get("ok") is True:
                log.info("기존 OK 폴링 결과 유지 — 실패 기록 생략 ref=%s", booking_ref)
                return
    except Exception:
        log.debug("실패 저장 전 OK 확인 실패(무시) ref=%s", booking_ref, exc_info=True)
    store_result(booking_ref, result, booking_type=booking_type, entity_id=entity_id)


def _integrity_err_msg(exc: pymysql.err.IntegrityError) -> str:
    try:
        return str(exc.args[1])
    except Exception:
        return ""


def _dup_key_is_sqs_booking_ref(exc: pymysql.err.IntegrityError) -> bool:
    m = _integrity_err_msg(exc)
    return "uq_concert_booking_sqs_ref" in m or "sqs_booking_ref" in m


def _mysql_integrity_errno(exc: pymysql.err.IntegrityError) -> int:
    try:
        return int(exc.args[0])
    except Exception:
        return 0


# ── 콘서트 홀드 종료(점유/원복) ────────────────────────────────────────────────
def _concert_seat_key(show_id: int, row: int, col: int) -> str:
    return f"concert:seat:{int(show_id)}:{int(row)}-{int(col)}:hold:v1"


def _concert_reserved_set_key(show_id: int) -> str:
    # legacy (확정/홀드 혼용) — 현재는 hold 전용 set을 사용한다.
    return f"concert:reserved:{int(show_id)}:v1"


def _concert_hold_set_key(show_id: int) -> str:
    return f"concert:hold:{int(show_id)}:v1"


def _concert_hold_meta_key(booking_ref: str) -> str:
    return f"concert:holdmeta:{booking_ref}:v1"

def _concert_confirmed_set_key(show_id: int) -> str:
    return f"concert:confirmed:{int(show_id)}:v1"

def _concert_pending_key(show_id: int) -> str:
    return f"concert:show:{int(show_id)}:pending:v1"


def _concert_remain_count_key(show_id: int) -> str:
    # remain_count 단일 카운터(단일 진실)
    # 주의: Redis key suffix는 레거시 호환을 위해 ':remain:v1'를 유지한다.
    return f"concert:show:{int(show_id)}:remain:v1"


_PENDING_ADJUST_LUA = """
local v = redis.call('INCRBY', KEYS[1], tonumber(ARGV[1]))
if v < 0 then
  redis.call('SET', KEYS[1], 0)
  v = 0
end
return v
"""


def _adjust_concert_pending(show_id: int, delta: int) -> None:
    d = _to_int(delta, 0)
    if show_id <= 0 or d == 0:
        return
    try:
        elasticache_read_cache_client.eval(_PENDING_ADJUST_LUA, 1, _concert_pending_key(int(show_id)), int(d))
    except Exception:
        return


def _adjust_concert_remain(show_id: int, delta: int) -> None:
    d = _to_int(delta, 0)
    if show_id <= 0 or d == 0:
        return
    try:
        elasticache_read_cache_client.eval(_PENDING_ADJUST_LUA, 1, _concert_remain_count_key(int(show_id)), int(d))
    except Exception:
        return

def _to_epoch_seconds(dt_like) -> int:
    """
    pymysql DATETIME → epoch seconds (local tz naive는 서버 로컬 기준으로 처리).
    실패하면 0.
    """
    try:
        import datetime as _dt

        if isinstance(dt_like, _dt.datetime):
            return int(dt_like.timestamp())
        if isinstance(dt_like, _dt.date):
            return int(_dt.datetime(dt_like.year, dt_like.month, dt_like.day).timestamp())
        # string fallback
        s = str(dt_like or "").strip().replace("T", " ")
        if not s:
            return 0
        # accept "YYYY-MM-DD HH:MM:SS"
        base = s[:19]
        parsed = _dt.datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
        return int(parsed.timestamp())
    except Exception:
        return 0


def _finalize_concert_hold_by_ref(booking_ref: str, *, restore_remain: bool) -> int:
    """
    write-api에서 잡아둔 Redis 홀드를 worker 처리 완료 시점에 정리한다.
    요구사항:
    1) 백그라운드 처리중: reserved set에 들어간 좌석은 회색(예약불가) + remain 감소에 즉시 반영
    2) DB write 실패(중복좌석/오류 등): hold를 풀어 원복(회색 해제 + remain 회복)
    3) DB write 성공: 좌석은 "확정" 상태이므로 회색/감소는 유지. (DB ACTIVE가 최종 근거)
       → 이때는 seat hold key/meta만 정리하고 reserved set membership은 유지한다.
    """
    ref = str(booking_ref or "").strip()
    if not ref:
        return
    try:
        raw = elasticache_read_cache_client.get(_concert_hold_meta_key(ref))
    except Exception:
        return
    if not raw:
        return
    try:
        meta = json.loads(raw)
    except Exception:
        return
    try:
        show_id = int(meta.get("show_id") or 0)
    except Exception:
        show_id = 0
    if show_id <= 0:
        return
    seats = meta.get("seats") or []
    parsed = []
    for s in seats:
        p = _parse_seat_key(s)
        if p:
            parsed.append(p)
    if not parsed:
        try:
            elasticache_read_cache_client.delete(_concert_hold_meta_key(ref))
        except Exception:
            pass
        return 0

    # 내가 잡은 홀드만 정리(booking_ref 일치 확인 후)
    released = 0
    try:
        pipe = elasticache_read_cache_client.pipeline()
        for r, c in parsed:
            pipe.get(_concert_seat_key(show_id, r, c))
        existing = pipe.execute()

        pipe2 = elasticache_read_cache_client.pipeline()
        set_key = _concert_hold_set_key(show_id)
        for (r, c), v in zip(parsed, existing):
            if str(v or "") == ref:
                seat_key = _concert_seat_key(show_id, r, c)
                if restore_remain:
                    # 실패/롤백: 홀드 자체를 제거(재시도/다른 유저 홀드 가능)
                    pipe2.delete(seat_key)
                else:
                    # 성공: 좌석은 확정이므로 Redis 좌석 키는 남겨 2중 방어(DB/Redis)로 중복 홀드를 막는다.
                    # value는 'CONFIRMED'로 바꿔 hold/booking_ref 추적과 구분한다.
                    # TTL이 있더라도 공연 종료 전까지는 유지되는 편이 안전하므로 PERSIST로 만료를 제거한다.
                    pipe2.set(seat_key, "CONFIRMED")
                    try:
                        pipe2.persist(seat_key)
                    except Exception:
                        pass
                # 성공/실패 모두 hold set에서는 제거 (주황 해제)
                pipe2.srem(set_key, f"{int(r)}-{int(c)}")
                released += 1
        pipe2.delete(_concert_hold_meta_key(ref))
        pipe2.execute()
        # 홀드 집합 변경 → read-api 경량 폴링(hold_rev)이 한 번에 따라오게
        try:
            elasticache_read_cache_client.incr(f"concert:show:{int(show_id)}:hold_rev:v1")
        except Exception:
            pass
    except Exception:
        return 0

    # 좌석/잔여가 바뀌었으니 회차 스냅샷 무효화(성공/실패 모두)
    try:
        elasticache_read_cache_client.delete(f"concert:show:{int(show_id)}:read:v2")
    except Exception:
        pass

    # Legacy behavior: remain is an operational Redis counter.
    # - 성공(restore_remain=False): hold 단계에서 이미 감소한 remain을 유지
    # - 실패/롤백(restore_remain=True): 해제된 수량만큼 remain을 복구
    if restore_remain and released > 0:
        _adjust_concert_remain(int(show_id), int(released))

    return int(released)


def _handle_one_sqs_message(msg: dict) -> bool:
    """
    True  → 처리 완료(또는 이미 처리된 중복)로 메시지 삭제(ACK)해도 됨.
    False → 삭제하지 않음 → 가시성 타임아웃 후 재전달 → maxReceiveCount 초과 시 DLQ.
    """
    receipt = msg.get("ReceiptHandle")
    if not receipt:
        log.warning("SQS 메시지에 ReceiptHandle 없음")
        return False

    attrs = msg.get("Attributes") or {}
    try:
        rc = int(attrs.get("ApproximateReceiveCount", "1"))
    except (TypeError, ValueError):
        rc = 1
    if rc >= 2:
        log.warning(
            "SQS 재전달 수신 ApproximateReceiveCount=%s MessageId=%s",
            rc,
            msg.get("MessageId"),
        )

    try:
        body = json.loads(msg["Body"])
    except (KeyError, TypeError, json.JSONDecodeError):
        log.exception("SQS 메시지 Body JSON 파싱 실패 MessageId=%s", msg.get("MessageId"))
        return False

    ref = body.get("booking_ref")
    if isinstance(ref, str) and ref:
        try:
            if elasticache_booking_client.get(_booking_result_key(ref)):
                log.info("이미 결과가 있는 booking_ref=%s — 중복 전달 ACK", ref)
                return True
        except Exception:
            log.exception("Redis 중복 확인 실패 — 처리 진행")

    t0 = time.monotonic()
    sem_wait_ms = 0.0
    try:
        booking_type = body.get("booking_type", "theater")
        t_sem = time.monotonic()
        with _db_sem:
            sem_wait_ms = (time.monotonic() - t_sem) * 1000.0
            _maybe_sleep_demo_delay()
            if booking_type == "concert":
                process_concert_booking(body)
            else:
                process_theater_booking(body)
        _stats.record_ok((time.monotonic() - t0) * 1000.0, sem_wait_ms)
        return True
    except Exception as e:
        _stats.record_fail((time.monotonic() - t0) * 1000.0, sem_wait_ms, str(e))
        log.exception("예매 핸들러 실패 MessageId=%s ref=%s", msg.get("MessageId"), ref)
        return False


def _delete_message(receipt_handle: str) -> None:
    sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)


# ── 극장 예매 처리 ────────────────────────────────────────────────────────────
def process_theater_booking(body):
    booking_ref = body["booking_ref"]
    user_id = _to_int(body["user_id"])
    schedule_id = _to_int(body["schedule_id"])
    seats = body.get("seats") or []

    parsed_seats = [_parse_seat_key(s) for s in seats]
    parsed_seats = [s for s in parsed_seats if s]
    req_count = len(parsed_seats)

    theater_id_for_cache = 0
    committed_ok = False
    conn = get_tx_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.schedule_id, s.hall_id, h.theater_id, s.total_count, s.remain_count "
                "FROM schedules s "
                "INNER JOIN halls h ON h.hall_id = s.hall_id "
                "WHERE s.schedule_id = %s FOR UPDATE",
                (schedule_id,),
            )
            schedule = cur.fetchone()
            if not schedule:
                store_result(booking_ref, {"ok": False, "code": "NOT_FOUND"}, booking_type="theater", entity_id=schedule_id)
                return

            theater_id_for_cache = _to_int(schedule.get("theater_id"))
            hall_id = _to_int(schedule.get("hall_id"))

            seat_ids = []
            for row_no, col_no in parsed_seats:
                cur.execute(
                    "SELECT seat_id FROM hall_seats "
                    "WHERE hall_id = %s AND seat_row_no = %s AND seat_col_no = %s",
                    (hall_id, row_no, col_no),
                )
                seat_row = cur.fetchone()
                if not seat_row:
                    conn.rollback()
                    store_result(booking_ref, {"ok": False, "code": "INVALID_SEAT"}, booking_type="theater", entity_id=schedule_id)
                    return
                seat_ids.append(_to_int(seat_row.get("seat_id")))

            cur.execute(
                "UPDATE schedules SET remain_count = remain_count - %s "
                "WHERE schedule_id = %s AND remain_count >= %s",
                (req_count, schedule_id, req_count),
            )
            if cur.rowcount != 1:
                conn.rollback()
                store_result(booking_ref, {"ok": False, "code": "SOLD_OUT"}, booking_type="theater", entity_id=schedule_id)
                return

            cur.execute(
                "INSERT INTO booking (user_id, schedule_id, reg_count, book_status) "
                "VALUES (%s, %s, %s, 'PAID')",
                (user_id, schedule_id, req_count),
            )
            booking_id = cur.lastrowid

            booking_code = ""
            for _ in range(12):
                code = _generate_booking_code()
                try:
                    cur.execute("UPDATE booking SET booking_code = %s WHERE booking_id = %s", (code, booking_id))
                    booking_code = code
                    break
                except pymysql.err.IntegrityError:
                    continue

            for seat_id in seat_ids:
                cur.execute(
                    "INSERT INTO booking_seats (booking_id, schedule_id, seat_id) "
                    "VALUES (%s, %s, %s)",
                    (booking_id, schedule_id, seat_id),
                )

            cur.execute(
                "INSERT INTO payment (booking_id, pay_yn, paid_at) "
                "VALUES (%s, 'Y', NOW())",
                (booking_id,),
            )
            payment_id = cur.lastrowid

            cur.execute("SELECT remain_count FROM schedules WHERE schedule_id = %s", (schedule_id,))
            remain = cur.fetchone()
            remain_count_after = _to_int(remain.get("remain_count") if remain else 0)

            if remain_count_after <= 0:
                cur.execute("UPDATE schedules SET status = 'CLOSED' WHERE schedule_id = %s", (schedule_id,))

        conn.commit()
        committed_ok = True
        store_result(booking_ref, {
            "ok": True, "code": "OK",
            "booking_id": booking_id, "booking_code": booking_code,
            "payment_id": payment_id, "remain_count_after": remain_count_after,
        }, booking_type="theater", entity_id=schedule_id)

    except pymysql.err.IntegrityError:
        conn.rollback()
        store_result(booking_ref, {"ok": False, "code": "DUPLICATE_SEAT"}, booking_type="theater", entity_id=schedule_id)
    except Exception as e:
        conn.rollback()
        store_result(booking_ref, {"ok": False, "code": "ERROR", "message": str(e)}, booking_type="theater", entity_id=schedule_id)
        log.error("극장 예매 처리 실패: %s", e)
    finally:
        conn.close()

    # 극장 read 캐시: 부트스트랩 1키에 전 스케줄 잔여가 들 있음 → 성공 시 항상 무효화.
    # (theater_id 가 0이면 예전 코드는 부트스트랩도 안 지워 UI가 30/30에 고정되는 경우가 있음)
    if committed_ok:
        try:
            keys = [THEATERS_BOOTSTRAP_CACHE_KEY]
            if theater_id_for_cache > 0:
                keys.append(_theaters_detail_cache_key(theater_id_for_cache))
            elasticache_read_cache_client.delete(*keys)
        except Exception:
            pass


def _concert_delete_read_caches(*, concert_id: int, show_id: int) -> None:
    try:
        cid = int(concert_id)
        sid = int(show_id)
        if cid > 0 and sid > 0:
            elasticache_read_cache_client.delete(
                f"concert:bootstrap:{cid}:read:v1",
                f"concert:show:{sid}:read:v2",
            )
    except Exception:
        pass


def _concert_poll_ok_from_db_if_complete(booking_ref: str, user_id: int, show_id: int, parsed_seats: list) -> bool:
    """
    좌석 유니크 등으로 IntegrityError 난 뒤, 동일 sqs_booking_ref 행이 이미 전부 커밋됐는지 확인해 OK만 재기록한다.
    (컬럼/인덱스 마이그레이션 전 환경에서는 매칭 행이 없어 False만 반환한다.)
    """
    want = {(int(r), int(c)) for r, c in parsed_seats}
    conn = get_tx_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cb.booking_id, cb.booking_code, cs.concert_id "
                "FROM concert_booking cb "
                "INNER JOIN concert_shows cs ON cs.show_id = cb.show_id "
                "WHERE cb.sqs_booking_ref = %s AND cb.user_id = %s AND cb.show_id = %s LIMIT 1",
                (booking_ref, user_id, show_id),
            )
            b = cur.fetchone()
            if not b:
                return False
            booking_id = int(b["booking_id"])
            booking_code = str(b.get("booking_code") or "")
            concert_id = _to_int(b.get("concert_id"))
            cur.execute(
                "SELECT seat_row_no, seat_col_no FROM concert_booking_seats "
                "WHERE booking_id = %s AND show_id = %s AND UPPER(COALESCE(status,'')) = 'ACTIVE'",
                (booking_id, show_id),
            )
            have = {(int(r["seat_row_no"]), int(r["seat_col_no"])) for r in cur.fetchall()}
            if have != want:
                return False
            cur.execute(
                "SELECT payment_id FROM concert_payment WHERE booking_id = %s LIMIT 1",
                (booking_id,),
            )
            pr = cur.fetchone()
            payment_id = int(pr["payment_id"]) if pr else 0
        try:
            raw_prev = elasticache_booking_client.get(_booking_result_key(booking_ref))
            if raw_prev:
                prev = json.loads(raw_prev)
                if prev.get("ok") is True:
                    _finalize_concert_hold_by_ref(booking_ref, restore_remain=False)
                    _concert_delete_read_caches(concert_id=concert_id, show_id=show_id)
                    log.info("콘서트 멱등 복구: 폴링 OK 이미 있음 ref=%s", booking_ref)
                    return True
        except Exception:
            pass
        try:
            raw_remain = elasticache_read_cache_client.get(_concert_remain_count_key(show_id))
            remain_count_after = _to_int(raw_remain, 0)
        except Exception:
            remain_count_after = 0
        store_result(
            booking_ref,
            {
                "ok": True,
                "code": "OK",
                "booking_id": booking_id,
                "booking_code": booking_code,
                "payment_id": payment_id,
                "remain_count_after": remain_count_after,
            },
            booking_type="concert",
            entity_id=show_id,
        )
        _finalize_concert_hold_by_ref(booking_ref, restore_remain=False)
        _concert_delete_read_caches(concert_id=concert_id, show_id=show_id)
        log.info("콘서트 멱등 복구: DB에 이미 완료된 예매 ref=%s booking_id=%s", booking_ref, booking_id)
        return True
    except Exception:
        log.exception("콘서트 멱등 복구 조회 실패 ref=%s", booking_ref)
        return False
    finally:
        conn.close()


# ── 콘서트 예매 처리 ─────────────────────────────────────────────────────────
def process_concert_booking(body):
    # Revert to the earlier "Redis remain counter is source-of-truth" behavior.
    booking_ref = body["booking_ref"]
    user_id = _to_int(body["user_id"])
    show_id = _to_int(body["show_id"])
    seats = body.get("seats") or []
    pending_count = _to_int(body.get("pending_count"), 0)

    parsed_seats = [_parse_seat_key(s) for s in seats]
    parsed_seats = [s for s in parsed_seats if s]
    req_count = len(parsed_seats)

    import secrets, string

    # 동시 워커가 같은 회차에 INSERT/UPDATE 하면 InnoDB deadlock(1213)이 날 수 있음.
    # WAS 이슈가 아니라 DB 락 순서; MySQL 권장: 짧은 트랜잭션 + deadlock 시 재시도.
    max_deadlock_retries = _get_int("WORKER_DB_DEADLOCK_RETRIES", 5, 1)
    attempt = 0
    while True:
        attempt += 1
        conn = get_tx_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT show_id, concert_id, show_date, seat_rows, seat_cols, "
                    "total_count, remain_count, status "
                    "FROM concert_shows WHERE show_id = %s",
                    (show_id,),
                )
                show = cur.fetchone()
                if not show:
                    conn.rollback()
                    store_result(booking_ref, {"ok": False, "code": "NOT_FOUND"}, booking_type="concert", entity_id=show_id)
                    _finalize_concert_hold_by_ref(booking_ref, restore_remain=True)
                    if pending_count > 0:
                        _adjust_concert_remain(show_id, pending_count)
                    return

                seat_rows = _to_int(show.get("seat_rows"))
                seat_cols = _to_int(show.get("seat_cols"))

                for row_no, col_no in parsed_seats:
                    if row_no > seat_rows or col_no > seat_cols:
                        conn.rollback()
                        store_result(booking_ref, {"ok": False, "code": "INVALID_SEAT"}, booking_type="concert", entity_id=show_id)
                        _finalize_concert_hold_by_ref(booking_ref, restore_remain=True)
                        if pending_count > 0:
                            _adjust_concert_remain(show_id, pending_count)
                        return

                booking_id: int | None = None
                booking_code = ""

                try:
                    cur.execute(
                        "INSERT INTO concert_booking (user_id, show_id, reg_count, book_status, sqs_booking_ref) "
                        "VALUES (%s, %s, %s, 'PAID', %s)",
                        (user_id, show_id, req_count, booking_ref),
                    )
                    booking_id = int(cur.lastrowid)
                except pymysql.err.IntegrityError as e_ins:
                    if not _dup_key_is_sqs_booking_ref(e_ins):
                        raise
                    conn.rollback()
                    cur.execute(
                        "SELECT booking_id, booking_code, user_id, show_id FROM concert_booking "
                        "WHERE sqs_booking_ref = %s LIMIT 1",
                        (booking_ref,),
                    )
                    b0 = cur.fetchone()
                    if not b0 or _to_int(b0.get("user_id")) != user_id or _to_int(b0.get("show_id")) != show_id:
                        log.warning("sqs_booking_ref 중복 행이 요청과 불일치 ref=%s", booking_ref)
                        raise
                    booking_id = int(b0["booking_id"])
                    booking_code = str(b0.get("booking_code") or "")
                    cur.execute(
                        "SELECT seat_row_no, seat_col_no FROM concert_booking_seats "
                        "WHERE booking_id = %s AND show_id = %s AND UPPER(COALESCE(status,'')) = 'ACTIVE'",
                        (booking_id, show_id),
                    )
                    have = {(int(r["seat_row_no"]), int(r["seat_col_no"])) for r in cur.fetchall()}
                    want = {(int(r), int(c)) for r, c in parsed_seats}
                    if have == want:
                        cur.execute(
                            "SELECT payment_id FROM concert_payment WHERE booking_id = %s LIMIT 1",
                            (booking_id,),
                        )
                        pr = cur.fetchone()
                        payment_id = int(pr["payment_id"]) if pr else 0
                        conn.commit()
                        try:
                            raw_prev = elasticache_booking_client.get(_booking_result_key(booking_ref))
                            if raw_prev and json.loads(raw_prev).get("ok") is True:
                                _finalize_concert_hold_by_ref(booking_ref, restore_remain=False)
                                if pending_count > 0:
                                    _adjust_concert_pending(show_id, -pending_count)
                                _concert_delete_read_caches(
                                    concert_id=_to_int(show.get("concert_id")), show_id=show_id
                                )
                                log.info("콘서트 멱등: sqs_booking_ref 재전달·DB 이미 완료 ref=%s", booking_ref)
                                return
                        except Exception:
                            pass
                        try:
                            raw_remain = elasticache_read_cache_client.get(_concert_remain_count_key(show_id))
                            remain_count_after = _to_int(raw_remain, 0)
                        except Exception:
                            remain_count_after = 0
                        store_result(
                            booking_ref,
                            {
                                "ok": True,
                                "code": "OK",
                                "booking_id": booking_id,
                                "booking_code": booking_code,
                                "payment_id": payment_id,
                                "remain_count_after": remain_count_after,
                            },
                            booking_type="concert",
                            entity_id=show_id,
                        )
                        _finalize_concert_hold_by_ref(booking_ref, restore_remain=False)
                        if pending_count > 0:
                            _adjust_concert_pending(show_id, -pending_count)
                        _concert_delete_read_caches(concert_id=_to_int(show.get("concert_id")), show_id=show_id)
                        return
                    if have:
                        conn.rollback()
                        store_failure_result(
                            booking_ref,
                            {"ok": False, "code": "ERROR", "message": "idempotent_seat_mismatch"},
                            booking_type="concert",
                            entity_id=show_id,
                        )
                        _finalize_concert_hold_by_ref(booking_ref, restore_remain=True)
                        if pending_count > 0:
                            _adjust_concert_remain(show_id, pending_count)
                            _adjust_concert_pending(show_id, -pending_count)
                        return

                if booking_id is None:
                    conn.rollback()
                    store_failure_result(
                        booking_ref,
                        {"ok": False, "code": "ERROR", "message": "no_booking_id"},
                        booking_type="concert",
                        entity_id=show_id,
                    )
                    _finalize_concert_hold_by_ref(booking_ref, restore_remain=True)
                    if pending_count > 0:
                        _adjust_concert_remain(show_id, pending_count)
                        _adjust_concert_pending(show_id, -pending_count)
                    return

                if not booking_code:
                    for _ in range(12):
                        letters = "".join(secrets.choice(string.ascii_uppercase) for _ in range(2))
                        digits = "".join(secrets.choice(string.digits) for _ in range(6))
                        code = f"C{letters}{digits}"
                        try:
                            cur.execute(
                                "UPDATE concert_booking SET booking_code = %s WHERE booking_id = %s",
                                (code, booking_id),
                            )
                            booking_code = code
                            break
                        except pymysql.err.IntegrityError:
                            continue

                values_sql = ",".join(["(%s,%s,%s,%s)"] * len(parsed_seats))
                params: list = []
                for row_no, col_no in parsed_seats:
                    params.extend([booking_id, show_id, row_no, col_no])
                cur.execute(
                    "INSERT INTO concert_booking_seats (booking_id, show_id, seat_row_no, seat_col_no) "
                    f"VALUES {values_sql}",
                    tuple(params),
                )

                cur.execute(
                    "INSERT INTO concert_payment (booking_id, pay_yn, paid_at) "
                    "VALUES (%s, 'Y', NOW())",
                    (booking_id,),
                )
                payment_id = cur.lastrowid

                try:
                    raw_remain = elasticache_read_cache_client.get(_concert_remain_count_key(show_id))
                    remain_count_after = _to_int(raw_remain, 0)
                except Exception:
                    remain_count_after = 0

                # NOTE: concert_shows status 업데이트는 핫스팟(모든 요청이 같은 1행)이라
                # 트랜잭션 내에서 매번 건드리면 데드락이 급증한다.
                # sold-out일 때만 커밋 후 best-effort로 CLOSED 반영한다.

            conn.commit()

            store_result(
                booking_ref,
                {
                    "ok": True,
                    "code": "OK",
                    "booking_id": booking_id,
                    "booking_code": booking_code,
                    "payment_id": payment_id,
                    "remain_count_after": remain_count_after,
                },
                booking_type="concert",
                entity_id=show_id,
            )
            _finalize_concert_hold_by_ref(booking_ref, restore_remain=False)
            if pending_count > 0:
                _adjust_concert_pending(show_id, -pending_count)

            _concert_delete_read_caches(concert_id=_to_int(show.get("concert_id")), show_id=show_id)
            _maybe_close_concert_show_if_soldout(show_id, remain_count_after)
            return

        except pymysql.err.OperationalError as oe:
            conn.rollback()
            if _is_mysql_deadlock(oe) and attempt < max_deadlock_retries:
                log.warning(
                    "콘서트 예매 MySQL deadlock 재시도 (%s/%s) ref=%s",
                    attempt,
                    max_deadlock_retries,
                    booking_ref,
                )
                time.sleep(
                    min(0.5, 0.02 * (2 ** (attempt - 1))) + random.uniform(0, 0.025)
                )
                continue
            store_failure_result(
                booking_ref,
                {"ok": False, "code": "ERROR", "message": str(oe)},
                booking_type="concert",
                entity_id=show_id,
            )
            _finalize_concert_hold_by_ref(booking_ref, restore_remain=True)
            if pending_count > 0:
                _adjust_concert_remain(show_id, pending_count)
                _adjust_concert_pending(show_id, -pending_count)
            log.error("콘서트 예매 OperationalError: %s", oe)
            return

        except pymysql.err.IntegrityError as ie:
            conn.rollback()
            errno = _mysql_integrity_errno(ie)
            # 1062 = duplicate key (좌석/uq 등), 1452 = FK 위반 — 후자를 DUPLICATE_SEAT로 오표기하면
            # users 미시드·스키마 불일치 시 전부 "중복좌석"처럼 보인다.
            if errno == 1452:
                store_failure_result(
                    booking_ref,
                    {
                        "ok": False,
                        "code": "FK_VIOLATION",
                        "message": _integrity_err_msg(ie) or "foreign key constraint failed",
                    },
                    booking_type="concert",
                    entity_id=show_id,
                )
                _finalize_concert_hold_by_ref(booking_ref, restore_remain=True)
                if pending_count > 0:
                    _adjust_concert_remain(show_id, pending_count)
                    _adjust_concert_pending(show_id, -pending_count)
                log.warning("콘서트 예매 FK 실패 ref=%s user_id=%s show_id=%s: %s", booking_ref, user_id, show_id, ie)
            elif _concert_poll_ok_from_db_if_complete(booking_ref, user_id, show_id, parsed_seats):
                pass
            else:
                store_failure_result(
                    booking_ref,
                    {"ok": False, "code": "DUPLICATE_SEAT"},
                    booking_type="concert",
                    entity_id=show_id,
                )
                _finalize_concert_hold_by_ref(booking_ref, restore_remain=True)
                if pending_count > 0:
                    _adjust_concert_remain(show_id, pending_count)
                    _adjust_concert_pending(show_id, -pending_count)
            log.debug("콘서트 IntegrityError errno=%s: %s", errno, ie)
        except Exception as e:
            conn.rollback()
            store_failure_result(
                booking_ref,
                {"ok": False, "code": "ERROR", "message": str(e)},
                booking_type="concert",
                entity_id=show_id,
            )
            _finalize_concert_hold_by_ref(booking_ref, restore_remain=True)
            if pending_count > 0:
                _adjust_concert_remain(show_id, pending_count)
                _adjust_concert_pending(show_id, -pending_count)
            log.error("콘서트 예매 처리 실패: %s", e)
        finally:
            conn.close()


# ── SQS 폴링 루프 ────────────────────────────────────────────────────────────
def _process_received_batch(messages: list) -> None:
    """배치 내 메시지는 서로 다른 MessageGroupId일 수 있어 병렬 처리해도 FIFO 순서를 깨지 않음."""
    if not messages:
        return

    def _ack_if_ok(msg: dict, ok: bool) -> None:
        receipt = msg.get("ReceiptHandle")
        if ok and receipt:
            try:
                _delete_message(receipt)
                _stats.inc_acked()
            except Exception:
                log.exception(
                    "SQS delete_message 실패 (메시지가 재전달될 수 있음) id=%s",
                    msg.get("MessageId"),
                )

    if len(messages) <= 1 or WORKER_SQS_BATCH_CONCURRENCY <= 1:
        for msg in messages:
            _ack_if_ok(msg, _handle_one_sqs_message(msg))
        return

    # 전역 스레드풀에서 처리(배치별 생성 비용 제거). workers 제한은 submit 수로 자연 제한됨.
    future_to_msg = {_batch_pool.submit(_handle_one_sqs_message, msg): msg for msg in messages}
    for fut in as_completed(future_to_msg):
        msg = future_to_msg[fut]
        try:
            ok = fut.result()
        except Exception:
            log.exception(
                "워커 배치 처리 예외 MessageId=%s",
                msg.get("MessageId"),
            )
            ok = False
        _ack_if_ok(msg, ok)


def poll_loop(poller_id: int = 0):
    log.info(
        "worker-svc 시작(poller=%s) — SQS url=%s max_msg=%s batch_workers=%s pollers=%s db_max_conc=%s wait=%ss | "
        "ElastiCache cache_db=%s booking_db=%s CACHE_ENABLED=%s BOOKING_STATE_ENABLED=%s",
        poller_id,
        SQS_QUEUE_URL,
        SQS_RECEIVE_MAX_MESSAGES,
        WORKER_SQS_BATCH_CONCURRENCY,
        WORKER_SQS_POLLERS,
        WORKER_DB_MAX_CONCURRENT,
        SQS_WAIT_TIME_SECONDS,
        ELASTICACHE_LOGICAL_DB_CACHE,
        ELASTICACHE_LOGICAL_DB_BOOKING,
        CACHE_ENABLED,
        BOOKING_STATE_ENABLED,
    )
    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=SQS_RECEIVE_MAX_MESSAGES,
                WaitTimeSeconds=SQS_WAIT_TIME_SECONDS,
                AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
            )
            messages = resp.get("Messages") or []
            if messages:
                _stats.inc_received(len(messages))
            _process_received_batch(messages)
        except Exception as e:
            _stats.record_fail(0.0, 0.0, str(e))
            log.error("SQS 폴링 오류: %s", e)
            time.sleep(SQS_POLL_ERROR_BACKOFF_SEC)


# ── FastAPI (헬스체크 + 메트릭) ───────────────────────────────────────────────
from fastapi import FastAPI
from contextlib import asynccontextmanager
import threading


@asynccontextmanager
async def lifespan(app):
    threads = []
    # remain → DB sync loop (optional)
    if REMAIN_DB_SYNC_ENABLED:
        t_sync = threading.Thread(target=_remain_db_sync_loop, daemon=True)
        t_sync.start()
        threads.append(t_sync)
    for i in range(WORKER_SQS_POLLERS):
        t = threading.Thread(target=poll_loop, args=(i,), daemon=True)
        t.start()
        threads.append(t)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "worker-svc"}


@app.get("/metrics")
def metrics():
    """
    데모/운영 관측용 JSON 메트릭.
    - 처리량(TPS)은 외부에서 5~10초 간격으로 diff를 내면 됨.
    - DB 세마포어 대기(avg/max)가 올라가면 DB 동시성/인덱스/경합 이슈 신호.
    """
    return _stats.snapshot()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5002")))
