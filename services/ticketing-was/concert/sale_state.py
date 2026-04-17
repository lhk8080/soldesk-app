from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

from cache.redis_client import redis_client


def _sale_key(show_id: int) -> str:
    return f"concert:sale:{int(show_id)}:v1"


def set_sale_state(show_id: int, status: str, *, close_at_epoch_ms: int | None = None) -> None:
    st = (status or "").strip().upper()
    if st not in ("OPEN", "CLOSED"):
        st = "OPEN"
    payload = {
        "status": st,
        "close_at_epoch_ms": int(close_at_epoch_ms) if close_at_epoch_ms else None,
        "updated_at_epoch_ms": int(time.time() * 1000),
    }
    redis_client.set(_sale_key(show_id), json.dumps(payload, ensure_ascii=False))


def get_sale_state(show_id: int) -> Dict:
    raw = redis_client.get(_sale_key(show_id))
    if not raw:
        return {"status": "OPEN", "close_at_epoch_ms": None}
    try:
        j = json.loads(raw)
        if not isinstance(j, dict):
            return {"status": "OPEN", "close_at_epoch_ms": None}
        st = str(j.get("status") or "OPEN").upper()
        if st not in ("OPEN", "CLOSED"):
            st = "OPEN"
        ca = j.get("close_at_epoch_ms")
        try:
            ca = int(ca) if ca is not None else None
        except Exception:
            ca = None
        return {"status": st, "close_at_epoch_ms": ca}
    except json.JSONDecodeError:
        return {"status": "OPEN", "close_at_epoch_ms": None}


def is_open(show_id: int) -> bool:
    return str(get_sale_state(show_id).get("status") or "OPEN").upper() == "OPEN"


def mget_sale_states(show_ids: List[int]) -> Dict[str, Dict]:
    if not show_ids:
        return {}
    keys = [_sale_key(int(sid)) for sid in show_ids]
    raws = redis_client.mget(keys)
    out: Dict[str, Dict] = {}
    for sid, raw in zip(show_ids, raws):
        if not raw:
            out[str(int(sid))] = {"status": "OPEN", "close_at_epoch_ms": None}
            continue
        try:
            j = json.loads(raw)
            if not isinstance(j, dict):
                raise ValueError("not dict")
            st = str(j.get("status") or "OPEN").upper()
            if st not in ("OPEN", "CLOSED"):
                st = "OPEN"
            ca = j.get("close_at_epoch_ms")
            try:
                ca = int(ca) if ca is not None else None
            except Exception:
                ca = None
            out[str(int(sid))] = {"status": st, "close_at_epoch_ms": ca}
        except Exception:
            out[str(int(sid))] = {"status": "OPEN", "close_at_epoch_ms": None}
    return out

