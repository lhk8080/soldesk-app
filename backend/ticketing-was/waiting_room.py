from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

from cache.elasticache_booking_client import elasticache_booking_client
from config import (
    QUEUE_ADMIT_RATE_PER_SEC,
    QUEUE_PERMIT_TTL_SEC,
    QUEUE_REF_TTL_SEC,
    WR_COUNTER_TTL_SEC,
    WR_AUTO_DRAIN_SEC,
    WR_AUTO_MAX_RATE,
    WR_AUTO_MIN_RATE,
    WR_AUTO_WINDOW_SEC,
    WR_OBSERVE_TTL_SEC,
    WR_BYPASS_BACKLOG,
    WR_BYPASS_RPS,
    WR_QUEUE_ON_BACKLOG,
    WR_QUEUE_ON_RPS,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _enq_key(kind: str, entity_id: int) -> str:
    return f"wr:{kind}:{int(entity_id)}:enq"


def _done_key(kind: str, entity_id: int) -> str:
    return f"wr:{kind}:{int(entity_id)}:done"


def _clock_key(kind: str, entity_id: int) -> str:
    return f"wr:{kind}:{int(entity_id)}:clock"


def _control_key(kind: str, entity_id: int) -> str:
    return f"wr:{kind}:{int(entity_id)}:control"

def _observe_key(kind: str, entity_id: int) -> str:
    return f"wr:{kind}:{int(entity_id)}:observe"

def _rps_bucket_key(kind: str, entity_id: int, epoch_sec: int) -> str:
    return f"wr:{kind}:{int(entity_id)}:rps:{int(epoch_sec)}"


def reset(*, kind: str, entity_id: int) -> Dict[str, Any]:
    """
    Waiting Room 카운터를 "서버가 실제로 사용하는 Redis"에서 리셋한다.
    - 테스트 도구(tools-once)에서 terraform output으로 Redis 엔드포인트를 유추하는 방식은
      실제 write-api/worker가 사용하는 Secret(ELASTICACHE_PRIMARY_ENDPOINT)과 불일치할 수 있다.
    - 이 함수는 write-api 내부에서 실행되므로, 실제 서비스 Redis(booking 논리 DB)에 확실히 적용된다.
    """
    kind = str(kind or "").strip().lower()
    entity_id = int(entity_id or 0)
    if kind not in ("concert", "theater") or entity_id <= 0:
        return {"ok": False, "code": "BAD_REQUEST"}

    deleted_fixed = 0
    deleted_rps = 0
    try:
        deleted_fixed = int(
            elasticache_booking_client.delete(
                _enq_key(kind, entity_id),
                _done_key(kind, entity_id),
                _clock_key(kind, entity_id),
                _control_key(kind, entity_id),
                _observe_key(kind, entity_id),
            )
            or 0
        )
    except Exception:
        deleted_fixed = 0

    try:
        # scan_iter는 단일 노드 Redis에서 충분히 안전하게 동작
        match = f"wr:{kind}:{int(entity_id)}:rps:*"
        batch = []
        for k in elasticache_booking_client.scan_iter(match=match, count=500):
            batch.append(k)
            if len(batch) >= 500:
                deleted_rps += int(elasticache_booking_client.delete(*batch) or 0)
                batch = []
        if batch:
            deleted_rps += int(elasticache_booking_client.delete(*batch) or 0)
    except Exception:
        deleted_rps = 0

    return {
        "ok": True,
        "kind": kind,
        "entity_id": int(entity_id),
        "deleted_fixed": int(deleted_fixed),
        "deleted_rps": int(deleted_rps),
    }


def _control_snapshot(kind: str, entity_id: int) -> Dict[str, Any]:
    """
    외부 모니터링/운영툴이 주입할 수 있는 제어값.
    - enabled: false면 대기열을 "강제 유지"(입장 허가를 사실상 멈춤)
    - admit_rate_per_sec: 초당 입장 허가 인원(override)
    - message: UI에서 보여줄 운영 메시지(옵션)
    """
    raw = elasticache_booking_client.get(_control_key(kind, entity_id))
    if not raw:
        return {"mode": "AUTO", "enabled": True, "admit_rate_per_sec": int(QUEUE_ADMIT_RATE_PER_SEC), "message": ""}
    try:
        d = json.loads(raw)
    except Exception:
        return {"mode": "AUTO", "enabled": True, "admit_rate_per_sec": int(QUEUE_ADMIT_RATE_PER_SEC), "message": ""}
    if not isinstance(d, dict):
        return {"mode": "AUTO", "enabled": True, "admit_rate_per_sec": int(QUEUE_ADMIT_RATE_PER_SEC), "message": ""}
    mode = str(d.get("mode") or "AUTO").strip().upper()
    if mode not in ("AUTO", "MANUAL"):
        mode = "AUTO"
    enabled = bool(d.get("enabled", True))
    try:
        rate = int(d.get("admit_rate_per_sec") or QUEUE_ADMIT_RATE_PER_SEC)
    except Exception:
        rate = int(QUEUE_ADMIT_RATE_PER_SEC)
    rate = max(1, rate)
    msg = str(d.get("message") or "")
    return {"mode": mode, "enabled": enabled, "admit_rate_per_sec": rate, "message": msg}


def _observe_snapshot(kind: str, entity_id: int) -> Dict[str, Any]:
    raw = elasticache_booking_client.get(_observe_key(kind, entity_id))
    if not raw:
        return {"ok": False, "fresh": False}
    try:
        d = json.loads(raw)
    except Exception:
        return {"ok": False, "fresh": False}
    if not isinstance(d, dict):
        return {"ok": False, "fresh": False}
    return {"ok": True, "fresh": True, "data": d}


def _rps_estimate(kind: str, entity_id: int) -> float:
    """
    최근 WR_AUTO_WINDOW_SEC 동안 enter 호출 RPS(자체 계기).
    """
    w = max(1, int(WR_AUTO_WINDOW_SEC))
    now_sec = int(time.time())
    keys = [_rps_bucket_key(kind, entity_id, now_sec - i) for i in range(w)]
    try:
        vals = elasticache_booking_client.mget(keys) or []
    except Exception:
        return 0.0
    s = 0
    for v in vals:
        try:
            s += int(v or 0)
        except Exception:
            pass
    return float(s) / float(w)


def _auto_effective_rate(kind: str, entity_id: int) -> int:
    """
    AUTO 모드에서 admit_rate_per_sec 결정.
    우선순위:
    1) 외부 관측(observe)이 신선하면 그 값을 우선 반영 (모니터링 붙었을 때)
    2) 자체 RPS/backlog로 단순 제어 (모니터링 없이도)
    """
    mn = max(1, int(WR_AUTO_MIN_RATE))
    mx = max(mn, int(WR_AUTO_MAX_RATE))
    drain = max(10, int(WR_AUTO_DRAIN_SEC))

    # backlog
    try:
        enq = int(elasticache_booking_client.get(_enq_key(kind, entity_id)) or 0)
    except Exception:
        enq = 0
    try:
        done = int(elasticache_booking_client.get(_done_key(kind, entity_id)) or 0)
    except Exception:
        done = 0
    backlog = max(0, enq - done)

    obs = _observe_snapshot(kind, entity_id)
    if obs.get("fresh") and isinstance(obs.get("data"), dict):
        d = obs["data"]
        # 모니터링이 바로 "권장 rate"를 줄 수 있게 허용
        if "admit_rate_per_sec" in d:
            try:
                r = int(d.get("admit_rate_per_sec") or 0)
                if r > 0:
                    return max(mn, min(mx, r))
            except Exception:
                pass
        # 또는 "혼잡도/목표" 기반 값이 올 수도 있음 (확장 포인트)

    # 자체 판단(간단/시연 강함)
    rps = _rps_estimate(kind, entity_id)

    # 혼잡하지 않으면: 대기열을 사실상 우회(즉시 입장에 가깝게)
    if rps <= float(max(1, int(WR_BYPASS_RPS))) and backlog <= int(WR_BYPASS_BACKLOG):
        return mx

    # 혼잡 조건(둘 중 하나면 대기열을 '강하게' 보여줌)
    congested = (rps >= float(max(1, int(WR_QUEUE_ON_RPS)))) or (backlog >= int(WR_QUEUE_ON_BACKLOG))
    if congested and rps >= 500:
        return mn

    # 목표: backlog를 drain_sec 내로 빼되, 너무 급격하면 상한/하한으로 clamp
    rate_by_backlog = int((backlog / float(drain))) if backlog > 0 else mx
    # 폭주 구간에서는 더 보수적으로(연출 강화 + 시스템 보호)
    if congested and rps >= 200:
        return max(mn, min(mx, max(mn, rate_by_backlog // 2)))
    return max(mn, min(mx, max(mn, rate_by_backlog)))


def _ref_key(queue_ref: str) -> str:
    return f"wr:ref:{queue_ref}"


def _permit_key(permit_token: str) -> str:
    return f"wr:permit:{permit_token}"


def _advance_done(kind: str, entity_id: int) -> int:
    """
    입장 허가 진행(done)을 "시간 기반"으로 전진시킨다.
    - 외부 배치/크론 없이도 status 폴링이 들어오는 동안 자연스럽게 done이 증가함.
    - done은 DB 처리량과 무관한 '입장 게이트' 전용.
    """
    ctl = _control_snapshot(kind, entity_id)
    if not bool(ctl.get("enabled", True)):
        # 입장 허가 정지(대기열 유지)
        return int(elasticache_booking_client.get(_done_key(kind, entity_id)) or 0)
    mode = str(ctl.get("mode") or "AUTO").strip().upper()
    if mode == "MANUAL":
        rate = max(1, int(ctl.get("admit_rate_per_sec") or QUEUE_ADMIT_RATE_PER_SEC))
    else:
        rate = _auto_effective_rate(kind, entity_id)
    clock_k = _clock_key(kind, entity_id)
    done_k = _done_key(kind, entity_id)

    now = _now_ms()
    pipe = elasticache_booking_client.pipeline()
    pipe.get(clock_k)
    pipe.get(done_k)
    prev_clock_raw, done_raw = pipe.execute()

    try:
        prev_clock = int(prev_clock_raw or 0)
    except Exception:
        prev_clock = 0
    try:
        done = int(done_raw or 0)
    except Exception:
        done = 0

    if prev_clock <= 0:
        # 초기화
        elasticache_booking_client.set(clock_k, str(now))
        # 카운터 TTL 리프레시(운영/시연 누적 방지)
        try:
            ttl = int(WR_COUNTER_TTL_SEC)
            if ttl > 0:
                elasticache_booking_client.expire(clock_k, ttl)
                elasticache_booking_client.expire(done_k, ttl)
        except Exception:
            pass
        return done

    elapsed_ms = max(0, now - prev_clock)
    inc = (elapsed_ms // 1000) * rate
    if inc <= 0:
        return done

    new_done = done + int(inc)
    pipe2 = elasticache_booking_client.pipeline()
    pipe2.set(clock_k, str(prev_clock + int((elapsed_ms // 1000) * 1000)))
    pipe2.set(done_k, str(new_done))
    # TTL을 함께 갱신(상태 조회가 들어오는 동안만 유지)
    try:
        ttl = int(WR_COUNTER_TTL_SEC)
        if ttl > 0:
            pipe2.expire(clock_k, ttl)
            pipe2.expire(done_k, ttl)
    except Exception:
        pass
    pipe2.execute()
    return new_done


def enter(*, kind: str, entity_id: int, user_id: int) -> Dict[str, Any]:
    """
    대기열 진입: queue_ref 발급 + seq(순번) 부여.
    """
    kind = str(kind or "").strip().lower()
    if kind not in ("concert", "theater"):
        return {"ok": False, "code": "BAD_KIND"}
    entity_id = int(entity_id or 0)
    user_id = int(user_id or 0)
    if entity_id <= 0 or user_id <= 0:
        return {"ok": False, "code": "BAD_REQUEST"}

    enq_k = _enq_key(kind, entity_id)
    seq = int(elasticache_booking_client.incr(enq_k) or 0)
    # 카운터 TTL 리프레시(운영/시연 누적 방지)
    try:
        ttl = int(WR_COUNTER_TTL_SEC)
        if ttl > 0:
            elasticache_booking_client.expire(enq_k, ttl)
    except Exception:
        pass
    # 자체 계기: 초 단위 enter RPS 버킷 (AUTO 판단용)
    try:
        sec = int(time.time())
        bkey = _rps_bucket_key(kind, entity_id, sec)
        elasticache_booking_client.incr(bkey)
        elasticache_booking_client.expire(bkey, max(2, int(WR_AUTO_WINDOW_SEC) + 2))
    except Exception:
        pass
    queue_ref = str(uuid.uuid4())
    meta = {
        "kind": kind,
        "entity_id": entity_id,
        "user_id": user_id,
        "seq": seq,
        "created_at_ms": _now_ms(),
    }
    elasticache_booking_client.setex(_ref_key(queue_ref), int(QUEUE_REF_TTL_SEC), json.dumps(meta, ensure_ascii=False))
    return {"ok": True, "code": "QUEUED", "queue_ref": queue_ref, "seq": seq}


def status(*, queue_ref: str) -> Dict[str, Any]:
    ref = str(queue_ref or "").strip()
    if not ref:
        return {"ok": False, "code": "INVALID_REF"}
    raw = elasticache_booking_client.get(_ref_key(ref))
    if not raw:
        return {"ok": False, "code": "UNKNOWN_OR_EXPIRED", "status": "UNKNOWN_OR_EXPIRED"}
    try:
        meta = json.loads(raw)
    except Exception:
        return {"ok": False, "code": "UNKNOWN_OR_EXPIRED", "status": "UNKNOWN_OR_EXPIRED"}

    kind = str(meta.get("kind") or "")
    entity_id = int(meta.get("entity_id") or 0)
    user_id = int(meta.get("user_id") or 0)
    seq = int(meta.get("seq") or 0)

    ctl = _control_snapshot(kind, entity_id)
    done = _advance_done(kind, entity_id)
    position = max(0, seq - done) if seq > 0 else 0
    ahead = max(0, position - 1) if position > 0 else 0

    # ETA(예상 대기시간): 현재 admit rate 기준으로 ahead를 소거하는 데 걸리는 시간
    mode = str(ctl.get("mode") or "AUTO").strip().upper()
    enabled = bool(ctl.get("enabled", True))
    if not enabled:
        rate = 0
    elif mode == "MANUAL":
        rate = max(1, int(ctl.get("admit_rate_per_sec") or QUEUE_ADMIT_RATE_PER_SEC))
    else:
        rate = int(_auto_effective_rate(kind, entity_id) or 0)
    eta_sec = None
    if rate > 0 and ahead > 0:
        eta_sec = int((ahead + rate - 1) // rate)

    if position <= 1 and seq > 0:
        # 입장 허가: 짧은 TTL의 permit 발급
        permit = str(uuid.uuid4())
        permit_meta = {
            "kind": kind,
            "entity_id": entity_id,
            "user_id": user_id,
            "issued_at_ms": _now_ms(),
        }
        elasticache_booking_client.setex(_permit_key(permit), int(QUEUE_PERMIT_TTL_SEC), json.dumps(permit_meta))
        return {
            "ok": True,
            "status": "ADMITTED",
            "queue_ref": ref,
            "permit_token": permit,
            "permit_ttl_sec": int(QUEUE_PERMIT_TTL_SEC),
            "control": ctl,
            "rate": int(rate),
            "eta_sec": eta_sec,
            "queue": {
                "kind": kind,
                "entity_id": entity_id,
                "seq": seq,
                "done": int(done),
                "position": position,
                "ahead": ahead,
            },
        }

    return {
        "ok": True,
        "status": "PROCESSING",
        "queue_ref": ref,
        "control": ctl,
        "rate": int(rate),
        "eta_sec": eta_sec,
        "queue": {
            "kind": kind,
            "entity_id": entity_id,
            "seq": seq,
            "done": int(done),
            "position": position,
            "ahead": ahead,
        },
    }


def metrics(*, kind: str, entity_id: int) -> Dict[str, Any]:
    kind = str(kind or "").strip().lower()
    entity_id = int(entity_id or 0)
    if kind not in ("concert", "theater") or entity_id <= 0:
        return {"ok": False, "code": "BAD_REQUEST"}
    ctl = _control_snapshot(kind, entity_id)
    obs = _observe_snapshot(kind, entity_id)
    try:
        enq = int(elasticache_booking_client.get(_enq_key(kind, entity_id)) or 0)
    except Exception:
        enq = 0
    # metrics 호출만으로도 게이트(done)가 시간 기반으로 전진해야 "대기 인원 감소"가 눈에 보인다.
    # (status 폴링이 끊겨도 모니터링/대시보드가 metrics만 치면 backlog가 자연 감소)
    try:
        done = int(_advance_done(kind, entity_id) or 0)
    except Exception:
        try:
            done = int(elasticache_booking_client.get(_done_key(kind, entity_id)) or 0)
        except Exception:
            done = 0
    rps = _rps_estimate(kind, entity_id)
    return {
        "ok": True,
        "kind": kind,
        "entity_id": entity_id,
        "enqueued": enq,
        "admitted": done,
        "backlog": max(0, enq - done),
        "enter_rps_est": round(rps, 2),
        "observe": obs.get("data") if obs.get("fresh") else None,
        "control": ctl,
    }


def set_control(*, kind: str, entity_id: int, enabled: Optional[bool], admit_rate_per_sec: Optional[int], message: Optional[str]) -> Dict[str, Any]:
    kind = str(kind or "").strip().lower()
    entity_id = int(entity_id or 0)
    if kind not in ("concert", "theater") or entity_id <= 0:
        return {"ok": False, "code": "BAD_REQUEST"}
    cur = _control_snapshot(kind, entity_id)
    nxt = dict(cur)
    # mode (AUTO|MANUAL)
    # - AUTO: admit_rate는 내부/외부 관측 기반으로 계산
    # - MANUAL: admit_rate_per_sec를 그대로 사용
    # NOTE: 여기서는 set_control이 mode를 직접 받지 않지만, payload의 message에 섞지 않게 라우트에서 넘겨주도록 설계.
    if enabled is not None:
        nxt["enabled"] = bool(enabled)
    if admit_rate_per_sec is not None:
        nxt["admit_rate_per_sec"] = max(1, int(admit_rate_per_sec))
    if message is not None:
        nxt["message"] = str(message)
    nxt["updated_at_ms"] = _now_ms()
    elasticache_booking_client.set(_control_key(kind, entity_id), json.dumps(nxt, ensure_ascii=False))
    return {"ok": True, "control": nxt}


def set_control_full(*, kind: str, entity_id: int, mode: Optional[str], enabled: Optional[bool], admit_rate_per_sec: Optional[int], message: Optional[str]) -> Dict[str, Any]:
    cur = _control_snapshot(kind, entity_id)
    nxt = dict(cur)
    if mode is not None:
        m = str(mode).strip().upper()
        if m in ("AUTO", "MANUAL"):
            nxt["mode"] = m
    if enabled is not None:
        nxt["enabled"] = bool(enabled)
    if admit_rate_per_sec is not None:
        nxt["admit_rate_per_sec"] = max(1, int(admit_rate_per_sec))
    if message is not None:
        nxt["message"] = str(message)
    nxt["updated_at_ms"] = _now_ms()
    elasticache_booking_client.set(_control_key(kind, entity_id), json.dumps(nxt, ensure_ascii=False))
    return {"ok": True, "control": nxt}


def observe(*, kind: str, entity_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    외부 모니터링/운영툴이 관측치를 주입하는 API용.
    예: {"admit_rate_per_sec": 25} 또는 추후 {"rps":..., "cpu":...} 등 확장 가능.
    """
    kind = str(kind or "").strip().lower()
    entity_id = int(entity_id or 0)
    if kind not in ("concert", "theater") or entity_id <= 0:
        return {"ok": False, "code": "BAD_REQUEST"}
    if not isinstance(data, dict):
        return {"ok": False, "code": "BAD_DATA"}
    payload = dict(data)
    payload["observed_at_ms"] = _now_ms()
    elasticache_booking_client.setex(_observe_key(kind, entity_id), int(WR_OBSERVE_TTL_SEC), json.dumps(payload, ensure_ascii=False))
    return {"ok": True, "ttl_sec": int(WR_OBSERVE_TTL_SEC)}


def verify_permit(*, permit_token: str, kind: str, entity_id: int, user_id: int) -> bool:
    tok = str(permit_token or "").strip()
    if not tok:
        return False
    raw = elasticache_booking_client.get(_permit_key(tok))
    if not raw:
        return False
    try:
        meta = json.loads(raw)
    except Exception:
        return False
    if str(meta.get("kind") or "") != str(kind):
        return False
    if int(meta.get("entity_id") or 0) != int(entity_id):
        return False
    if int(meta.get("user_id") or 0) != int(user_id):
        return False
    return True

