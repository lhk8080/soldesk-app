from __future__ import annotations

import logging
import os
from typing import Dict, List, Tuple

from cache.redis_client import redis_client
from config import CACHE_ENABLED
from db import get_db_connection

log = logging.getLogger(__name__)


def _chunks(items: List[int], size: int) -> List[List[int]]:
    if size <= 0:
        size = 200
    return [items[i : i + size] for i in range(0, len(items), size)]


def reconcile_concert_remain_on_startup() -> Dict:
    """
    서버 기동 시(개발/재배포/리셋 직후) DB를 기준으로 회차(show) remain을 동기화한다.

    동기화 내용:
    - DB concert_shows.remain_count: total - confirmed(ACTIVE) - holds(active)
    - Redis concert:show:{show_id}:remain:v1: 위 계산값으로 overwrite (CACHE_ENABLED=true일 때)
    - Redis confirmed set 정리: DB confirmed=0이면 confirmed set 삭제(빈 DB인데 Redis만 남아있는 phantom 방지)
    """
    enabled = str(os.getenv("CONCERT_RECONCILE_REMAIN_ON_STARTUP", "true") or "").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        return {"ok": True, "skipped": True, "reason": "disabled_by_env"}

    # read-api / write-api를 동시에 띄워도 동기화는 한 번만 실행되도록 DB 락을 건다.
    # Redis가 꺼진 환경에서도 동작해야 하므로 MySQL GET_LOCK을 사용한다.
    lock_name = "ticketing:startup:concert_remain_reconcile:v1"
    conn = get_db_connection()
    got_lock = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 0) AS got", (lock_name,))
            got_lock = int((cur.fetchone() or {}).get("got") or 0)
            if got_lock != 1:
                return {"ok": True, "skipped": True, "reason": "lock_not_acquired"}

            cur.execute("SELECT show_id, total_count FROM concert_shows")
            shows = cur.fetchall() or []
            show_ids = [int(r["show_id"]) for r in shows if int(r.get("show_id") or 0) > 0]
            total_by_show = {int(r["show_id"]): int(r.get("total_count") or 0) for r in shows if int(r.get("show_id") or 0) > 0}

            confirmed_count: Dict[int, int] = {}
            holds_count: Dict[int, int] = {}

            if show_ids:
                for batch in _chunks(show_ids, 200):
                    placeholders = ",".join(["%s"] * len(batch))
                    cur.execute(
                        f"SELECT show_id, COUNT(*) AS n "
                        f"FROM concert_booking_seats "
                        f"WHERE show_id IN ({placeholders}) AND UPPER(COALESCE(status,''))='ACTIVE' "
                        f"GROUP BY show_id",
                        tuple(batch),
                    )
                    for r in cur.fetchall() or []:
                        confirmed_count[int(r["show_id"])] = int(r.get("n") or 0)

                    # DB hold 폴백 테이블이 있는 경우만 count (없으면 0)
                    try:
                        cur.execute(
                            f"SELECT show_id, COUNT(*) AS n "
                            f"FROM concert_seat_holds "
                            f"WHERE show_id IN ({placeholders}) AND (expires_at IS NULL OR expires_at > NOW()) "
                            f"GROUP BY show_id",
                            tuple(batch),
                        )
                        for r in cur.fetchall() or []:
                            holds_count[int(r["show_id"])] = int(r.get("n") or 0)
                    except Exception:
                        # 테이블이 아직 없거나(초기화 전) 환경이 다른 경우 → holds=0으로 진행
                        pass

            # 계산 및 DB 업데이트 (CASE batch)
            updated_db = 0
            remain_by_show: Dict[int, int] = {}
            for sid in show_ids:
                total = int(total_by_show.get(sid) or 0)
                conf = int(confirmed_count.get(sid) or 0)
                hold = int(holds_count.get(sid) or 0)
                remain = max(0, total - conf - hold)
                remain_by_show[sid] = remain

            for batch in _chunks(show_ids, 200):
                cases = []
                params: List[int] = []
                for sid in batch:
                    cases.append("WHEN %s THEN %s")
                    params.extend([int(sid), int(remain_by_show.get(int(sid), 0))])
                params.extend(batch)
                sql = (
                    "UPDATE concert_shows SET remain_count = CASE show_id "
                    + " ".join(cases)
                    + " ELSE remain_count END "
                    + f"WHERE show_id IN ({','.join(['%s'] * len(batch))})"
                )
                cur.execute(sql, tuple(params))
                updated_db += int(cur.rowcount or 0)

    finally:
        if got_lock == 1:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    # Redis 반영(옵션)
    redis_written = 0
    confirmed_deleted = 0
    if CACHE_ENABLED:
        try:
            pipe = redis_client.pipeline()
            for sid, remain in remain_by_show.items():
                pipe.set(f"concert:show:{int(sid)}:remain:v1", int(remain))
                redis_written += 1
                if int(confirmed_count.get(int(sid), 0) or 0) == 0:
                    pipe.delete(f"concert:confirmed:{int(sid)}:v1")
                    confirmed_deleted += 1
            pipe.execute()
        except Exception:
            log.exception("startup reconcile: redis write failed")

    return {
        "ok": True,
        "skipped": False,
        "shows": len(remain_by_show),
        "db_updated_rows": int(updated_db),
        "redis_written": int(redis_written),
        "redis_confirmed_deleted_when_empty": int(confirmed_deleted),
    }

