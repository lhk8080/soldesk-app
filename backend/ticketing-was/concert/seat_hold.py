from __future__ import annotations

import json
import logging
import time
from typing import Dict, List, Tuple

from cache.redis_client import redis_client
from config import (
    CONCERT_CONFIRMED_SET_TTL_SEC,
    CONCERT_SEAT_HOLD_TTL_SEC,
    CONCERT_SEAT_HOLD_SOLDOUT_TTL_SEC,
)

log = logging.getLogger(__name__)


def _seat_key(show_id: int, row: int, col: int) -> str:
    return f"concert:seat:{int(show_id)}:{int(row)}-{int(col)}:hold:v1"


def _remain_count_key(show_id: int) -> str:
    # remain_count 단일 카운터(단일 진실). read는 이 값만 신뢰한다.
    # 주의: Redis key suffix는 레거시 호환을 위해 ':remain:v1'를 유지한다.
    return f"concert:show:{int(show_id)}:remain:v1"


def _remain_dirty_set_key() -> str:
    # remain 카운터 변경된 회차를 모아, 비동기로 DB에 반영하기 위한 dirty set
    return "concert:remain_dirty:show_ids:v1"


_REMAIN_ADJUST_LUA = """
local v = redis.call('INCRBY', KEYS[1], tonumber(ARGV[1]))
if v < 0 then
  redis.call('SET', KEYS[1], 0)
  v = 0
end
return v
"""

_REMAIN_DECR_IF_ENOUGH_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then
  cur = 0
else
  cur = tonumber(cur) or 0
end
local n = tonumber(ARGV[1]) or 0
if n <= 0 then
  return cur
end
if cur < n then
  return -1
end
local v = redis.call('DECRBY', KEYS[1], n)
if v < 0 then
  redis.call('SET', KEYS[1], 0)
  v = 0
end
return v
"""


def adjust_remain(*, show_id: int, delta: int, ttl_sec: int | None = None) -> int:
    """
    remain_count 카운터를 원자적으로 조정한다.
    - delta < 0 : 차감(hold/pending)
    - delta > 0 : 복구(실패/취소/만료 롤백)
    """
    sid = int(show_id or 0)
    d = int(delta or 0)
    if sid <= 0 or d == 0:
        return 0
    try:
        v = int(redis_client.eval(_REMAIN_ADJUST_LUA, 1, _remain_count_key(sid), d) or 0)
        # DB remain 동기화 파이프라인용 dirty 표시 (best-effort)
        try:
            redis_client.sadd(_remain_dirty_set_key(), str(int(sid)))
        except Exception:
            pass
        # remain은 hold/pending과 연동되는 운영 카운터이므로 누적 방지용 TTL을 옵션으로 지원
        try:
            ttl = int(ttl_sec or 0)
            if ttl > 0:
                redis_client.expire(_remain_count_key(sid), ttl)
        except Exception:
            pass
        if int(v) <= 0 and d < 0:
            _expire_hold_cache_on_soldout(show_id=sid)
        return v
    except Exception:
        return 0


def try_decrease_remain_if_enough(*, show_id: int, count: int, ttl_sec: int | None = None) -> tuple[bool, int]:
    """
    remain_count가 충분할 때만 원자적으로 차감한다.
    - 성공: (True, remain_after)
    - 부족: (False, current_remain)  (Lua에서 -1로 signal)
    """
    sid = int(show_id or 0)
    n = int(count or 0)
    if sid <= 0 or n <= 0:
        return False, 0
    try:
        v = int(redis_client.eval(_REMAIN_DECR_IF_ENOUGH_LUA, 1, _remain_count_key(sid), n) or 0)
        if v < 0:
            # not enough
            try:
                cur = int(redis_client.get(_remain_count_key(sid)) or 0)
            except Exception:
                cur = 0
            return False, max(0, int(cur))
        # DB remain 동기화 파이프라인용 dirty 표시 (best-effort)
        try:
            redis_client.sadd(_remain_dirty_set_key(), str(int(sid)))
        except Exception:
            pass
        try:
            ttl = int(ttl_sec or 0)
            if ttl > 0:
                redis_client.expire(_remain_count_key(sid), ttl)
        except Exception:
            pass
        if int(v) <= 0:
            _expire_hold_cache_on_soldout(show_id=sid)
        return True, int(v)
    except Exception:
        try:
            cur = int(redis_client.get(_remain_count_key(sid)) or 0)
        except Exception:
            cur = 0
        return False, max(0, int(cur))


def _expire_hold_cache_on_soldout(*, show_id: int) -> None:
    """
    평소에는 홀드 TTL=무한(0)으로 유지하되, remain이 0(마감) 되는 순간에만
    홀드 관련 키들에 TTL을 걸어 자연 만료되게 한다.
    """
    sid = int(show_id or 0)
    if sid <= 0:
        return
    ttl = int(CONCERT_SEAT_HOLD_SOLDOUT_TTL_SEC or 0)
    if ttl <= 0:
        return

    # 메타 키 TTL
    try:
        pipe = redis_client.pipeline()
        pipe.expire(_hold_set_key(sid), ttl)
        pipe.expire(_hold_rev_key(sid), ttl)
        pipe.expire(_remain_count_key(sid), ttl)
        pipe.execute()
    except Exception:
        pass

    # 좌석 홀드 키 TTL (show_id prefix로 scan)
    try:
        pat = f"concert:seat:{sid}:*:hold:v1"
        batch: list[str] = []
        for k in redis_client.scan_iter(match=pat, count=1000):
            batch.append(str(k))
            if len(batch) >= 1000:
                p = redis_client.pipeline()
                for kk in batch:
                    p.expire(kk, ttl)
                p.execute()
                batch = []
        if batch:
            p = redis_client.pipeline()
            for kk in batch:
                p.expire(kk, ttl)
            p.execute()
    except Exception:
        pass

    # holdmeta TTL (booking_ref prefix라 show_id별 scan 불가 → JSON에서 show_id 확인)
    try:
        batch2: list[str] = []
        for k in redis_client.scan_iter(match="concert:holdmeta:*:v1", count=1000):
            kk = str(k)
            try:
                raw = redis_client.get(kk)
                meta = json.loads(raw) if raw else None
                if isinstance(meta, dict) and int(meta.get("show_id") or 0) == sid:
                    batch2.append(kk)
            except Exception:
                continue
            if len(batch2) >= 500:
                p = redis_client.pipeline()
                for x in batch2:
                    p.expire(x, ttl)
                p.execute()
                batch2 = []
        if batch2:
            p = redis_client.pipeline()
            for x in batch2:
                p.expire(x, ttl)
            p.execute()
    except Exception:
        pass


def _reserved_set_key(show_id: int) -> str:
    return f"concert:reserved:{int(show_id)}:v1"

def _confirmed_set_key(show_id: int) -> str:
    """확정(회색) 좌석 — 홀드(주황)가 덮어씌우는 것을 방지하는 가드용."""
    return f"concert:confirmed:{int(show_id)}:v1"


def _hold_set_key(show_id: int) -> str:
    # 처리중(홀드) 좌석만 별도로 관리해 UI에서 주황 표시 가능
    return f"concert:hold:{int(show_id)}:v1"


def _hold_rev_key(show_id: int) -> str:
    """홀드 집합이 바뀔 때마다 증가 — 클라이언트는 DB 없이 rev만 비교해 주황 UI를 갱신할 수 있다."""
    return f"concert:show:{int(show_id)}:hold_rev:v1"


def get_hold_revision(show_id: int) -> int:
    try:
        return int(redis_client.get(_hold_rev_key(show_id)) or 0)
    except Exception:
        return 0


def bump_hold_revision(show_id: int) -> int:
    try:
        k = _hold_rev_key(show_id)
        v = int(redis_client.incr(k) or 0)
        # hold_rev는 UI 동기화용 메타라 TTL 없이 누적되면 테스트/운영에서 값이 끝없이 커진다.
        # 홀드 TTL과 동일하게 유지(홀드 변동이 있는 동안만 살아있게).
        try:
            ttl = int(CONCERT_SEAT_HOLD_TTL_SEC)
            if ttl > 0:
                redis_client.expire(k, ttl)
        except Exception:
            pass
        return v
    except Exception:
        return 0


def add_confirmed_seats(*, show_id: int, seat_keys: List[str]) -> None:
    """
    worker(DB 커밋 성공)에서 확정 좌석을 기록.
    이 set은 write-api 홀드 전에 검사되어 confirmed->hold(회색->주황) 역전이 불가능해진다.
    """
    if not seat_keys:
        return
    try:
        pipe = redis_client.pipeline()
        sk = _confirmed_set_key(show_id)
        pipe.sadd(sk, *[str(x) for x in seat_keys])
        ttl = int(CONCERT_CONFIRMED_SET_TTL_SEC)
        if ttl > 0:
            pipe.expire(sk, ttl)
        pipe.execute()
    except Exception:
        return


def remove_confirmed_seats(*, show_id: int, seat_keys: List[str]) -> None:
    """
    확정(회색) 좌석 set에서 제거(환불/취소 시).
    - DB가 최종 근거이지만, write-api hold 단계(any_confirmed)에서 Redis confirmed set을 1차 가드로 사용하므로
      환불 시에도 best-effort로 제거해 중복좌석/홀드 불가 현상을 방지한다.
    """
    if not seat_keys:
        return
    try:
        sk = _confirmed_set_key(show_id)
        redis_client.srem(sk, *[str(x) for x in seat_keys])
    except Exception:
        return


def any_confirmed(*, show_id: int, seats: List[Tuple[int, int]]) -> bool:
    if not seats:
        return False

    # 0차 가드(무DB): worker-svc가 성공 시 좌석 키에 남기는 "CONFIRMED" 문자열.
    # commit 경로에서 read:v2 스냅샷을 자주 지우므로, 스냅샷/DB 폴백에만 의존하면 RDS가 터질 수 있다.
    try:
        pipe0 = redis_client.pipeline()
        for r, c in seats:
            pipe0.get(_seat_key(show_id, int(r), int(c)))
        vals = pipe0.execute() or []
        for v in vals:
            if str(v or "").strip().upper() == "CONFIRMED":
                return True
    except Exception:
        pass

    # confirmed set이 비어있다면(=아직 확정 좌석이 전혀 없다면) DB까지 갈 필요가 없다.
    # 대량 오픈 직후에는 이 케이스가 대부분이라, 여기서 DB fallback을 막아야 write(hold) 경로가 빨라진다.
    try:
        if int(redis_client.scard(_confirmed_set_key(show_id)) or 0) <= 0:
            return False
    except Exception:
        # scard 실패는 보수적으로 기존 로직으로 진행
        pass
    try:
        sk = _confirmed_set_key(show_id)
        pipe = redis_client.pipeline()
        for r, c in seats:
            pipe.sismember(sk, f"{int(r)}-{int(c)}")
        res = pipe.execute()
        if any(bool(x) for x in (res or [])):
            return True
    except Exception:
        res = None

    # 2차 가드(무DB): read 스냅샷에 confirmed_seats 가 있으면 그것도 반영해 confirmed→hold 역전을 막는다.
    # (cold start 등 confirmed set이 아직 덜 채워진 순간에도 회색 좌석에 홀드가 걸리는 것을 완화)
    try:
        raw = redis_client.get(f"concert:show:{int(show_id)}:read:v2")
        if not raw:
            raise ValueError("no snapshot")
        snap = json.loads(raw)
        if not isinstance(snap, dict):
            raise ValueError("bad snapshot")
        confirmed = snap.get("confirmed_seats")
        if not isinstance(confirmed, list) or not confirmed:
            raise ValueError("empty confirmed")
        confirmed_set = {str(x) for x in confirmed}
        for r, c in seats:
            if f"{int(r)}-{int(c)}" in confirmed_set:
                return True
    except Exception:
        # DB 폴백 제거: 스냅샷이 없을 때마다 커밋당 pymysql 연결은 RDS/커넥션 풀을 붕괴시키고
        # DB_UNAVAILABLE(503)로 "로스"처럼 보이게 만든다. 최종 정합은 좌석 NX + 워커 유니크로 맡긴다.
        # (Redis만 비우고 DB에 ACTIVE가 남은 테스트면 워커에서 DUPLICATE로 떨어진다.)
        return False
    return False


def _hold_meta_key(booking_ref: str) -> str:
    return f"concert:holdmeta:{booking_ref}:v1"


def try_hold_seats(
    *,
    show_id: int,
    seats: List[Tuple[int, int]],
    booking_ref: str,
    ttl_sec: int | None = None,
    adjust_remain_count: bool = True,
) -> Dict:
    """
    좌석을 Redis에서 선점(접수 확정)한다.
    - seat key: SET NX EX ttl (booking_ref)
    - hold set: SADD "r-c" (UI 주황 표시 + remain 즉시 반영용)
    - hold meta: booking_ref → {show_id, seats[]} (실패 롤백용)
    """
    ttl = int(ttl_sec if ttl_sec is not None else CONCERT_SEAT_HOLD_TTL_SEC)
    if ttl > 0:
        ttl = max(10, ttl)
    if not seats:
        return {"ok": False, "code": "NO_SEATS"}

    # 회색(확정) 좌석은 절대 주황(홀드)로 덮어씌우지 않는다.
    if any_confirmed(show_id=show_id, seats=seats):
        return {"ok": False, "code": "CONFIRMED_SEAT"}

    held: List[Tuple[int, int]] = []
    pipe = redis_client.pipeline()
    for r, c in seats:
        if ttl > 0:
            pipe.set(_seat_key(show_id, r, c), booking_ref, nx=True, ex=ttl)
        else:
            pipe.set(_seat_key(show_id, r, c), booking_ref, nx=True)
    results = pipe.execute()

    for (r, c), ok in zip(seats, results):
        if ok:
            held.append((r, c))
        else:
            # 하나라도 실패하면 전부 롤백(요청 단위 원자성)
            release_seats(show_id=show_id, seats=held, booking_ref=booking_ref)
            return {"ok": False, "code": "DUPLICATE_SEAT"}

    # 홀드 좌석 set 갱신 (remain 즉시 반영 + UI 주황 표시)
    pipe2 = redis_client.pipeline()
    set_key = _hold_set_key(show_id)
    for r, c in held:
        pipe2.sadd(set_key, f"{int(r)}-{int(c)}")
    if ttl > 0:
        pipe2.expire(set_key, ttl)
    meta = {
        "show_id": int(show_id),
        "seats": [f"{int(r)}-{int(c)}" for r, c in held],
        "ttl_sec": ttl,
        "created_at_epoch_ms": int(time.time() * 1000),
    }
    if ttl > 0:
        pipe2.setex(_hold_meta_key(booking_ref), ttl, json.dumps(meta, ensure_ascii=False))
    else:
        pipe2.set(_hold_meta_key(booking_ref), json.dumps(meta, ensure_ascii=False))
    pipe2.execute()
    # Redis remain 카운터는 홀드 시점에 즉시 차감(UX).
    # (트랜스크립트 상 "아까 잘 되던" 방식)
    if adjust_remain_count:
        adjust_remain(show_id=show_id, delta=-len(held), ttl_sec=ttl)
    bump_hold_revision(show_id)
    return {"ok": True, "code": "HELD", "ttl_sec": ttl}


def release_seats_on_refund(*, show_id: int, seats: List[Tuple[int, int]]) -> None:
    """환불 시 confirmed 상태의 seat hold 키들을 강제로 삭제.

    release_seats() 는 booking_ref 가 일치해야만 삭제하지만 (실패/타임아웃 hold 해제용),
    환불은 이미 예매 확정(CONFIRMED) 된 좌석을 되돌리는 것이므로 booking_ref 대조 없이
    해당 show_id + (row,col) 키를 무조건 비워야 다음 사용자가 재예매 가능.

    DB 의 concert_booking_seats.status 는 CANCEL 로 업데이트되었다는 전제.
    Redis 잔여 CONFIRMED 키 때문에 409 DUPLICATE_SEAT 로 막히는 증상을 방지.
    """
    if not seats:
        return
    try:
        pipe = redis_client.pipeline()
        set_key = _hold_set_key(show_id)
        for r, c in seats:
            pipe.delete(_seat_key(show_id, int(r), int(c)))
            pipe.srem(set_key, f"{int(r)}-{int(c)}")
        pipe.execute()
        bump_hold_revision(show_id)
    except Exception:
        log.exception("release_seats_on_refund failed show_id=%s", show_id)


def release_seats(*, show_id: int, seats: List[Tuple[int, int]], booking_ref: str) -> None:
    if not seats:
        return
    pipe = redis_client.pipeline()
    for r, c in seats:
        pipe.get(_seat_key(show_id, r, c))
    existing = pipe.execute()

    pipe2 = redis_client.pipeline()
    set_key = _hold_set_key(show_id)
    released = 0
    for (r, c), v in zip(seats, existing):
        # 내가 잡은 홀드만 해제(다른 booking_ref의 홀드는 건드리지 않음)
        if str(v or "") == str(booking_ref):
            pipe2.delete(_seat_key(show_id, r, c))
            pipe2.srem(set_key, f"{int(r)}-{int(c)}")
            released += 1
    pipe2.execute()
    if released > 0:
        adjust_remain(show_id=show_id, delta=int(released))
    bump_hold_revision(show_id)


def hold_seats_snapshot(show_id: int) -> List[str]:
    try:
        return list(redis_client.smembers(_hold_set_key(show_id)) or [])
    except Exception:
        return []


def hold_count(show_id: int) -> int:
    try:
        return int(redis_client.scard(_hold_set_key(show_id)) or 0)
    except Exception:
        return 0


# reserved_* 는 "확정 좌석" 개념으로 유지하고 싶지만,
# 현재 시스템은 DB ACTIVE가 최종 근거이므로 read-cache에서 DB를 통해 확보한다.

def reserved_seats_snapshot(show_id: int) -> List[str]:
    try:
        return list(redis_client.smembers(_reserved_set_key(show_id)) or [])
    except Exception:
        return []


def reserved_count(show_id: int) -> int:
    try:
        return int(redis_client.scard(_reserved_set_key(show_id)) or 0)
    except Exception:
        return 0

