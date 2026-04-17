"""
콘서트 조회용 Redis 캐시.

부트스트랩 응답은 API 형태로는 { concert, shows[] } 이지만, 변동이 큰 데이터는
회차(show_id)별 스냅샷 키(`concert:show:{id}:read:v2`)에만 둔다. 예매 1건당 이 키만 무효화·재적재.

기동 웜업(CONCERT_CACHE_WARMUP_MODE=minimal): 공연 목록만 Redis; 공연 상세·회차 스냅샷은 요청 시 적재.
회차 행 목록은 `concert:shows_meta:{concert_id}:read:v1` 로 짧게 캐시해 부트스트랩 DB QPS를 줄인다.
스냅샷 재적재 시 DB에서 해당 show 1행을 항상 다시 읽어 잔여·상태를 맞춘다(배치 reserved + shows_meta 조합에도 동일).
부트스트랩 MGET은 키가 많을 때를 대비해 청크(128)로 나눈다. show 락은 고정 샤드(1024)로 메모리 누수를 막는다.

올리지 않는 것: user_id, 이메일, 예매자 식별, 결제 식별자 등 회원/예매 PII.
"""
from __future__ import annotations

import json
import random
import threading
from typing import Any, Dict, List, Optional

from cache.redis_client import redis_client
from concert.seat_hold import hold_count, hold_seats_snapshot
from cache.elasticache_booking_client import elasticache_booking_client
from config import (
    CONCERT_CACHE_WARMUP_MODE,
    CONCERT_DETAIL_CACHE_TTL_SEC,
    CONCERT_SHOW_SNAPSHOT_TTL_JITTER_PCT,
    CONCERT_SHOW_SNAPSHOT_TTL_SEC,
    CONCERT_SHOWS_META_TTL_SEC,
    CONCERTS_LIST_CACHE_TTL_SEC,
)
from db import get_db_read_connection

CONCERTS_LIST_KEY = "concerts:list:read:v1"

def _confirmed_set_key(show_id: int) -> str:
    # worker-svc 규약과 동일
    return f"concert:confirmed:{int(show_id)}:v1"


def _fetch_confirmed_seat_keys_by_show_from_redis(show_ids: List[int]) -> Dict[str, List[str]]:
    """
    Redis confirmed set은 worker가 DB 커밋 직후 즉시 갱신한다.
    read-cache가 DB만 보면, hold 해제 → DB 반영 사이의 짧은 구간에 remain이 "올라갔다 내려가는" 플리커가 생길 수 있어
    confirmed는 DB + Redis를 합집합으로 본다(중복은 제거).
    """
    if not show_ids:
        return {}
    out: Dict[str, List[str]] = {}
    try:
        pipe = redis_client.pipeline()
        for sid in show_ids:
            pipe.smembers(_confirmed_set_key(int(sid)))
        res = pipe.execute() or []
        for sid, keys in zip(show_ids, res):
            if not keys:
                continue
            # keys는 "r-c" 문자열들
            out[str(int(sid))] = sorted(
                [str(x) for x in keys],
                key=lambda x: (int(str(x).split("-")[0]), int(str(x).split("-")[1])),
            )
    except Exception:
        return {}
    return out


def _concert_shows_meta_key(concert_id: int) -> str:
    """공연 단위 회차 행 목록(스냅샷과 별도). 예매로 잔여만 바뀌면 이 키는 무효화하지 않는다."""
    return f"concert:shows_meta:{int(concert_id)}:read:v1"

# 회차별 스냅샷 적재 동시성: 동일 show_id 직렬화. 고정 샤드로 Lock 딕셔너리 무한 증가 방지.
_SHOW_FILL_LOCK_SHARDS = 1024
_show_fill_lock_shards: tuple[threading.Lock, ...] = tuple(
    threading.Lock() for _ in range(_SHOW_FILL_LOCK_SHARDS)
)


def _lock_for_show_fill(show_id: int) -> threading.Lock:
    return _show_fill_lock_shards[abs(int(show_id)) % _SHOW_FILL_LOCK_SHARDS]


def _redis_mget_values(keys: List[str], chunk_size: int = 128) -> List[Optional[str]]:
    if not keys:
        return []
    out: List[Optional[str]] = []
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i : i + chunk_size]
        out.extend(redis_client.mget(chunk))
    return out


def _snapshot_ttl_seconds() -> Optional[int]:
    base = int(CONCERT_SHOW_SNAPSHOT_TTL_SEC)
    if base <= 0:
        return None
    j = max(0, min(50, int(CONCERT_SHOW_SNAPSHOT_TTL_JITTER_PCT)))
    lo = int(base * (100 - j) / 100)
    hi = int(base * (100 + j) / 100)
    lo = max(1, lo)
    hi = max(lo, hi)
    return random.randint(lo, hi)


def _concert_detail_key(concert_id: int) -> str:
    return f"concert:detail:{int(concert_id)}:read:v1"


def _concert_bootstrap_key(concert_id: int) -> str:
    """레거시 공연 단위 부트스트랩 키 — 무효화 시 삭제만 한다."""
    return f"concert:bootstrap:{int(concert_id)}:read:v1"


def _concert_show_snapshot_key(show_id: int) -> str:
    return f"concert:show:{int(show_id)}:read:v2"

def _pending_key(show_id: int) -> str:
    """
    SQS enqueue 시점에 선차감(예상 처리량)용 pending 카운터.
    - hold(주황)로 이미 차감된 요청은 pending을 올리지 않는다(중복 차감 방지).
    - worker가 처리 완료(성공/실패) 시점에 pending을 내려 최종 정합(성공은 confirmed/DB가 유지, 실패는 복구).
    """
    return f"concert:show:{int(show_id)}:pending:v1"


def _remain_count_key(show_id: int) -> str:
    # remain_count 단일 카운터(단일 진실)
    # 주의: Redis key suffix는 레거시 호환을 위해 ':remain:v1'를 유지한다.
    return f"concert:show:{int(show_id)}:remain:v1"


def _get_pending(show_id: int) -> int:
    # Legacy: pending is optional/UX-only. Default to 0 on read path.
    return 0


def _get_remain(show_id: int) -> int:
    try:
        v = redis_client.get(_remain_count_key(int(show_id)))
        return max(0, int(v or 0))
    except Exception:
        return 0


def _get_or_seed_remain_from_row(row: Dict[str, Any]) -> int:
    """
    remain_count는 단일 카운터(단일 진실)만 사용한다.
    - 카운터 키가 없을 때만 DB remain_count로 1회 seed(setnx)
    - reserved/hold/pending 등으로 재계산하지 않는다(엉뚱한 remain 유입 방지)
    """
    sid = int(row.get("show_id") or 0)
    if sid <= 0:
        return 0
    try:
        raw = redis_client.get(_remain_count_key(sid))
        if raw is None:
            seed = max(0, int(row.get("remain_count") or 0))
            if seed > 0:
                try:
                    redis_client.setnx(_remain_count_key(sid), int(seed))
                except Exception:
                    pass
            return int(seed)
        v = max(0, int(raw or 0))
        try:
            total = int(row.get("total_count") or 0)
        except Exception:
            total = 0
        return min(v, total) if total > 0 else v
    except Exception:
        return 0


def _serialize_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _fetch_concerts_from_db() -> List[Dict[str, Any]]:
    conn = get_db_read_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.concert_id, c.title, c.category, c.genre, c.venue_summary,
                    c.poster_url, c.runtime_minutes, c.synopsis, c.synopsis_line,
                    c.status, c.hide, s.next_show_date
                FROM concerts c
                LEFT JOIN (
                    SELECT concert_id, MIN(show_date) AS next_show_date
                    FROM concert_shows GROUP BY concert_id
                ) s ON s.concert_id = c.concert_id
                ORDER BY concert_id ASC
            """)
            rows = cur.fetchall() or []
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "concert_id": int(r["concert_id"]), "title": r.get("title"),
                "category": r.get("category"), "genre": r.get("genre"),
                "venue_summary": r.get("venue_summary"), "poster_url": r.get("poster_url"),
                "runtime_minutes": int(r.get("runtime_minutes") or 0),
                "synopsis": r.get("synopsis"), "synopsis_line": r.get("synopsis_line"),
                "status": r.get("status"), "hide": r.get("hide"),
                "next_show_date": _serialize_dt(r.get("next_show_date")),
            })
        return out
    finally:
        conn.close()


def _fetch_concert_row(concert_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_read_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT concert_id, title, category, genre, venue_summary, poster_url,
                    runtime_minutes, synopsis, synopsis_line, status, hide
                FROM concerts WHERE concert_id = %s
            """, (concert_id,))
            r = cur.fetchone()
        if not r:
            return None
        return {
            "concert_id": int(r["concert_id"]), "title": r.get("title"),
            "category": r.get("category"), "genre": r.get("genre"),
            "venue_summary": r.get("venue_summary"), "poster_url": r.get("poster_url"),
            "runtime_minutes": int(r.get("runtime_minutes") or 0),
            "synopsis": r.get("synopsis"), "synopsis_line": r.get("synopsis_line"),
            "status": r.get("status"), "hide": r.get("hide"),
            "release_date": None, "release_date_display": None,
        }
    finally:
        conn.close()


def _fetch_concert_show_rows(concert_id: int) -> List[Dict[str, Any]]:
    conn = get_db_read_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT show_id, concert_id, show_date, venue_name, venue_address,
                    hall_name, seat_rows, seat_cols, total_count, remain_count, price, status
                FROM concert_shows WHERE concert_id = %s ORDER BY show_date ASC
            """, (concert_id,))
            return list(cur.fetchall() or [])
    finally:
        conn.close()


def _get_show_rows_for_bootstrap(concert_id: int) -> List[Dict[str, Any]]:
    """
    부트스트랩용 회차 행 목록. Redis 메타 TTL>0이면 캐시(스냅샷과 독립; 예매로 스냅샷만 갱신).
    """
    ttl = int(CONCERT_SHOWS_META_TTL_SEC)
    if ttl <= 0:
        return _fetch_concert_show_rows(concert_id)
    key = _concert_shows_meta_key(concert_id)
    raw = redis_client.get(key)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            redis_client.delete(key)
    rows = _fetch_concert_show_rows(concert_id)
    if rows:
        val = json.dumps(rows, default=str, ensure_ascii=False)
        redis_client.set(key, val, ex=ttl)
    return rows


def _fetch_concert_show_row(concert_id: int, show_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_read_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT show_id, concert_id, show_date, venue_name, venue_address,
                    hall_name, seat_rows, seat_cols, total_count, remain_count, price, status
                FROM concert_shows WHERE concert_id = %s AND show_id = %s
            """, (concert_id, show_id))
            r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _fetch_confirmed_seat_keys_by_show(show_ids: List[int]) -> Dict[str, List[str]]:
    """DB에서 확정(ACTIVE) 좌석 목록."""
    conn = get_db_read_connection()
    try:
        placeholders = ",".join(["%s"] * len(show_ids))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT show_id, seat_row_no, seat_col_no FROM concert_booking_seats "
                f"WHERE show_id IN ({placeholders}) AND UPPER(COALESCE(status, '')) = 'ACTIVE' "
                f"ORDER BY show_id, seat_row_no, seat_col_no",
                tuple(show_ids),
            )
            rows = cur.fetchall() or []
        result: Dict[str, List[str]] = {}
        for r in rows:
            sid = str(int(r["show_id"]))
            key = f"{int(r['seat_row_no'])}-{int(r['seat_col_no'])}"
            result.setdefault(sid, []).append(key)
        # worker가 즉시 갱신하는 Redis confirmed set과 합치기(플리커 방지)
        redis_map = _fetch_confirmed_seat_keys_by_show_from_redis(show_ids)
        if not redis_map:
            return result
        for sid, rkeys in redis_map.items():
            if not rkeys:
                continue
            merged = set(result.get(sid, [])).union(set(rkeys))
            result[sid] = sorted(
                merged,
                key=lambda x: (int(str(x).split("-")[0]), int(str(x).split("-")[1])),
            )
        return result
    finally:
        conn.close()


def _fetch_hold_seat_keys_by_show(show_ids: List[int]) -> Dict[str, List[str]]:
    """Redis에서 처리중(홀드) 좌석 목록."""
    out: Dict[str, List[str]] = {}
    for sid in show_ids:
        keys = hold_seats_snapshot(int(sid))
        if keys:
            out[str(int(sid))] = sorted(keys, key=lambda x: (int(x.split("-")[0]), int(x.split("-")[1])))
    return out


def _show_payload_from_row(r: Dict[str, Any], *, confirmed_keys: List[str], hold_keys: List[str]) -> Dict[str, Any]:
    sid = int(r["show_id"])
    total = int(r.get("total_count") or 0)
    # confirmed_keys: 회색(확정) / hold_keys: 주황(처리중)
    hold = hold_count(sid)  # legacy numeric (필요 시 유지)
    pending = 0

    # remain은 단일 카운터만 신뢰한다(재계산 금지).
    # 단, 데모/리셋 직후 remain 키가 없을 수 있으므로(초기화),
    # 그 경우에만 DB의 remain_count로 "1회 seed"해 버튼이 CLOSED로 잠기는 것을 방지한다.
    remain = 0
    try:
        raw = redis_client.get(_remain_count_key(sid))
        if raw is None:
            seed = int(r.get("remain_count") or 0)
            if seed > 0:
                try:
                    redis_client.setnx(_remain_count_key(sid), seed)
                except Exception:
                    pass
            remain = max(0, int(seed))
        else:
            remain = max(0, int(raw or 0))
    except Exception:
        remain = 0
    return {
        "show_id": sid,
        "concert_id": int(r["concert_id"]),
        "show_date": _serialize_dt(r.get("show_date")),
        "venue_name": r.get("venue_name"),
        "venue_address": r.get("venue_address"),
        "hall_name": r.get("hall_name"),
        "seat_rows": int(r.get("seat_rows") or 0),
        "seat_cols": int(r.get("seat_cols") or 0),
        "total_count": total,
        # 점유(홀드) 좌석 수 — DB 확정과 별개로 UI에 즉시 반영되는 수량
        "hold_count": int(hold),
        "remain_count": remain,
        "price": int(r.get("price") or 0),
        # status는 remain 카운터를 기준으로만 만든다(수동/과거 값으로 인해 remain>0인데 CLOSED가 내려오는 꼬임 방지).
        "status": "CLOSED" if remain <= 0 else "OPEN",
        "hold_seats": hold_keys,
        "confirmed_seats": confirmed_keys,
        "reserved_seats": sorted(
            set(confirmed_keys).union(hold_keys),
            key=lambda x: (int(x.split("-")[0]), int(x.split("-")[1])),
        ),
    }


def _build_show_snapshot_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    sid = int(row["show_id"])
    confirmed_map = _fetch_confirmed_seat_keys_by_show([sid])
    hold_map = _fetch_hold_seat_keys_by_show([sid])
    payload = _show_payload_from_row(
        row,
        confirmed_keys=confirmed_map.get(str(sid), []),
        hold_keys=hold_map.get(str(sid), []),
    )
    return payload


def _store_show_snapshot(payload: Dict[str, Any]) -> None:
    sid = int(payload["show_id"])
    key = _concert_show_snapshot_key(sid)
    val = json.dumps(payload, default=str, ensure_ascii=False)
    ex = _snapshot_ttl_seconds()
    if ex is not None:
        redis_client.set(key, val, ex=ex)
    else:
        redis_client.set(key, val)


def _coalesced_fill_show_snapshot(
    row: Dict[str, Any],
    reserved_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Redis 미스 시에만: show_id 단위 락으로 동시에 한 번만 DB(또는 배치에서 넘긴 reserved)로 채운다.
    reserved_keys 가 있으면 좌석 IN 쿼리 생략(부트스트랩 배치 경로).
    """
    sid = int(row["show_id"])
    key = _concert_show_snapshot_key(sid)
    raw = redis_client.get(key)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            redis_client.delete(key)
    with _lock_for_show_fill(sid):
        raw = redis_client.get(key)
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                redis_client.delete(key)
        # 배치/단건 공통: 스냅샷을 새로 쓸 때는 항상 해당 회차 1행을 DB에서 다시 읽어
        # shows_meta 캐시·배치 reserved 경로 모두에서 잔여/상태 정합성을 맞춘다.
        cid = int(row.get("concert_id") or 0)
        if cid > 0:
            fr = _fetch_concert_show_row(cid, sid)
            if fr:
                row = fr
        payload = _build_show_snapshot_from_row(row)
        _store_show_snapshot(payload)
        return payload


def get_or_load_concert_show_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    """회차 1건 Redis 스냅샷 (miss 시 DB 좌석만 조회 후 적재, 프로세스 내 싱글플라이트)."""
    return _coalesced_fill_show_snapshot(row, reserved_keys=None)


def build_concert_detail_api_dict(concert_id: int) -> Optional[Dict[str, Any]]:
    concert = _fetch_concert_row(concert_id)
    if not concert:
        return None
    concert = dict(concert)
    concert["release_date_display"] = concert.get("venue_summary") or ""
    return {"concert": concert}


def get_concerts_list_cached_or_load() -> List[Dict[str, Any]]:
    raw = redis_client.get(CONCERTS_LIST_KEY)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            redis_client.delete(CONCERTS_LIST_KEY)
    rows = _fetch_concerts_from_db()
    val = json.dumps(rows, default=str, ensure_ascii=False)
    ttl = int(CONCERTS_LIST_CACHE_TTL_SEC)
    if ttl > 0:
        redis_client.set(CONCERTS_LIST_KEY, val, ex=ttl)
    else:
        redis_client.set(CONCERTS_LIST_KEY, val)
    return rows


def get_concert_detail_cached_or_load(concert_id: int) -> Optional[Dict[str, Any]]:
    key = _concert_detail_key(concert_id)
    raw = redis_client.get(key)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            redis_client.delete(key)
    payload = build_concert_detail_api_dict(concert_id)
    if payload is not None:
        val = json.dumps(payload, default=str, ensure_ascii=False)
        ttl = int(CONCERT_DETAIL_CACHE_TTL_SEC)
        if ttl > 0:
            redis_client.set(key, val, ex=ttl)
        else:
            redis_client.set(key, val)
    return payload


def _concert_header_for_bootstrap(concert_id: int) -> Optional[Dict[str, Any]]:
    detail = get_concert_detail_cached_or_load(concert_id)
    if not detail:
        return None
    concert = dict(detail["concert"])
    if not concert.get("release_date_display"):
        concert["release_date_display"] = concert.get("venue_summary") or ""
    return concert


def get_concert_bootstrap_cached_or_load(concert_id: int) -> Optional[Dict[str, Any]]:
    """목록·회차 메타는 DB 한 번, 회차별 잔여·선점은 show 스냅샷 키로 분리 (MGET 1회)."""
    concert = _concert_header_for_bootstrap(concert_id)
    if not concert:
        return None
    show_rows = _get_show_rows_for_bootstrap(concert_id)
    if not show_rows:
        return {"concert": concert, "shows": []}
    show_ids = [int(r["show_id"]) for r in show_rows]
    # remain_count는 단일 카운터(단일 진실)만 사용한다.
    # reserved/hold/pending으로 재계산하면, hold set이 꼬였을 때(예: 대량 잔존) remain이 2 같은 값으로 깨질 수 있다.
    confirmed_bulk_db = _fetch_confirmed_seat_keys_by_show(show_ids)
    # 처리중(홀드) 좌석은 변동이 잦아, 스냅샷 히트여도 Redis 최신값으로 덮어쓴다(주황 UI 즉시 반영용).
    hold_bulk_latest = _fetch_hold_seat_keys_by_show(show_ids)
    keys = [_concert_show_snapshot_key(int(r["show_id"])) for r in show_rows]
    raws = _redis_mget_values(keys)
    slot: List[Optional[Dict[str, Any]]] = [None] * len(show_rows)
    missed: List[tuple[int, Dict[str, Any]]] = []
    for i, (row, raw, key) in enumerate(zip(show_rows, raws, keys)):
        if raw:
            try:
                snap = json.loads(raw)
                if isinstance(snap, dict):
                    sid = str(int(row["show_id"]))
                    # 최신 hold_seats를 덮어쓰기(스냅샷 remain_count는 약간 stale일 수 있으나,
                    # 좌석 UI 주황/점유 표시는 최신 hold_seats가 더 중요하다.)
                    latest_hold = hold_bulk_latest.get(sid, [])
                    snap["hold_seats"] = latest_hold
                    # confirmed는 DB ACTIVE 좌석을 기준으로 통일한다.
                    snap["confirmed_seats"] = confirmed_bulk_db.get(sid, [])
                    try:
                        confirmed = snap.get("confirmed_seats") if isinstance(snap.get("confirmed_seats"), list) else []
                        reserved = set([str(x) for x in (confirmed or [])]).union(set(latest_hold))
                        snap["reserved_seats"] = sorted(
                            reserved,
                            key=lambda x: (int(str(x).split("-")[0]), int(str(x).split("-")[1])),
                        )
                        # remain은 단일 카운터만 사용 (재계산 금지)
                        snap["remain_count"] = int(_get_or_seed_remain_from_row(row))
                        snap["status"] = "CLOSED" if int(snap["remain_count"]) <= 0 else (snap.get("status") or "OPEN")
                    except Exception:
                        pass
                slot[i] = snap
                continue
            except json.JSONDecodeError:
                redis_client.delete(key)
        missed.append((i, row))
    if missed:
        miss_ids = [int(r["show_id"]) for _, r in missed]
        hold_bulk = _fetch_hold_seat_keys_by_show(miss_ids)
        for i, row in missed:
            sid = int(row["show_id"])
            slot[i] = _coalesced_fill_show_snapshot(
                row,
                reserved_keys=None,
            )
            # 스냅샷을 새로 채운 경우라면, 최신 hold/confirmed를 즉시 반영
            slot[i]["hold_seats"] = hold_bulk.get(str(sid), [])
            slot[i]["confirmed_seats"] = confirmed_bulk_db.get(str(sid), [])
            slot[i]["reserved_seats"] = sorted(set(slot[i]["hold_seats"]).union(slot[i]["confirmed_seats"]), key=lambda x: (int(x.split("-")[0]), int(x.split("-")[1])))
            slot[i]["remain_count"] = int(_get_or_seed_remain_from_row(row))
            slot[i]["status"] = "CLOSED" if int(slot[i]["remain_count"]) <= 0 else (slot[i].get("status") or "OPEN")
    shows_filled: List[Dict[str, Any]] = [s for s in slot if s is not None]
    return {"concert": concert, "shows": shows_filled}


def get_concert_bootstrap_for_show(concert_id: int, show_id: int) -> Optional[Dict[str, Any]]:
    """선택 회차만 캐시/재조회 (서버 동기화·폴링용)."""
    row = _fetch_concert_show_row(concert_id, show_id)
    if not row:
        return None
    concert = _concert_header_for_bootstrap(concert_id)
    if not concert:
        return None
    snap = get_or_load_concert_show_snapshot(row)
    snap = dict(snap) if isinstance(snap, dict) else {"show_id": int(show_id)}
    return {"concert": concert, "shows": [snap]}


def warmup_concert_caches() -> Dict[str, Any]:
    """
    서버 기동 시: 공연 목록(필수 메타)만 Redis에 두는 것이 기본(minimal).
    full 모드일 때만 공연별 상세 키를 일괄 적재(공연 수가 많으면 ElastiCache/기동 시간 부담).
    회차 스냅샷은 예매·부트스트랩 요청 시 per-show 적재 + TTL.
    """
    mode = (CONCERT_CACHE_WARMUP_MODE or "minimal").strip().lower()
    if mode not in ("full", "minimal"):
        mode = "minimal"
    rows = _fetch_concerts_from_db()
    list_val = json.dumps(rows, default=str, ensure_ascii=False)
    list_ttl = int(CONCERTS_LIST_CACHE_TTL_SEC)
    if list_ttl > 0:
        redis_client.set(CONCERTS_LIST_KEY, list_val, ex=list_ttl)
    else:
        redis_client.set(CONCERTS_LIST_KEY, list_val)
    n_detail = 0
    if mode == "full":
        for r in rows:
            cid = int(r["concert_id"])
            d = build_concert_detail_api_dict(cid)
            if d:
                dv = json.dumps(d, default=str, ensure_ascii=False)
                dt = int(CONCERT_DETAIL_CACHE_TTL_SEC)
                dk = _concert_detail_key(cid)
                if dt > 0:
                    redis_client.set(dk, dv, ex=dt)
                else:
                    redis_client.set(dk, dv)
                n_detail += 1
    return {
        "name": "concert_read",
        "warmup_mode": mode,
        "list_key": CONCERTS_LIST_KEY,
        "concert_count": len(rows),
        "detail_keys": n_detail,
        "bootstrap_keys": 0,
        "shows_meta_ttl_sec": CONCERT_SHOWS_META_TTL_SEC,
        "show_snapshot_ttl_sec": CONCERT_SHOW_SNAPSHOT_TTL_SEC,
        "show_snapshot_ttl_jitter_pct": CONCERT_SHOW_SNAPSHOT_TTL_JITTER_PCT,
        "note": "invalidation on booking: show snapshot only; list/detail not flushed per ticket",
    }


def invalidate_concert_caches_after_booking(concert_id: int, show_id: Optional[int] = None) -> None:
    """
    예매/환불 후: **해당 회차 스냅샷**만 삭제(대량 티켓팅 시 전역 목록/상세 키를 지우지 않음).
    레거시 공연 단위 부트스트랩 키가 남아 있으면 함께 제거.
    공연 메타(제목 등)를 바꾼 뒤 즉시 반영하려면 관리용 전체 리빌드 또는 별도 무효화가 필요하다.
    """
    keys: List[str] = [_concert_bootstrap_key(concert_id)]
    sid = int(show_id or 0)
    if sid > 0:
        keys.append(_concert_show_snapshot_key(sid))
    try:
        redis_client.delete(*keys)
    except Exception:
        pass


def invalidate_concert_catalog_caches(concert_id: int) -> None:
    """공연 메타·회차 목록 캐시까지 비울 때(관리/배포용). 일반 예매 경로에서는 호출하지 않는다."""
    keys: List[str] = [
        CONCERTS_LIST_KEY,
        _concert_detail_key(concert_id),
        _concert_bootstrap_key(concert_id),
        _concert_shows_meta_key(concert_id),
    ]
    try:
        redis_client.delete(*keys)
    except Exception:
        pass
