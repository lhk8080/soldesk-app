#!/usr/bin/env python3
"""
콘서트 "10초에 N천명 쓰기 시도" 연출용 부하 생성기 v4 (개선판)

사용법:
  -n 50  →  50 * 1000 = 50,000건  (천 단위 입력)
  -n 30  →  30,000건

실행 예:
  WRITE_API_BASE_URL="http://write-api.ticketing.svc.cluster.local:5001" \\
  python3 sqs_load_real_concert5.py --show-id 8 -n 50 --duration-sec 10 \\
    --user-base 1 --http-concurrency 2000
  (--seat-rows / --seat-cols 생략 시 해당 회차 DB seat_rows·seat_cols 사용)

준비물:
  pip install aiohttp pymysql
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter, deque
from pathlib import Path
from shutil import which
from urllib.request import urlopen
from typing import Optional

# ──────────────────────────────────────────────
# http_booking_client 의존 (기존 코드 유지)
# ──────────────────────────────────────────────
try:
    import http_booking_client as http_w
except ImportError:
    http_w = None  # 로컬 테스트 시 None 허용 (아래에서 guard)


# ──────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────
BURST_UNIT = 1000          # -n 50 → 50,000건
DEFAULT_CONCERT_TITLE = "2026 봄 페스티벌 LIVE - 5만석"


# ──────────────────────────────────────────────
# 인프라 헬퍼
# ──────────────────────────────────────────────

def _terraform_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "terraform"


def _terraform_output_raw(name: str) -> str:
    tf_dir = _terraform_dir()
    if not tf_dir.is_dir():
        return ""
    try:
        proc = subprocess.run(
            ["terraform", "output", "-raw", name],
            cwd=str(tf_dir),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _resolve_db_writer_host() -> str:
    h = (os.getenv("DB_WRITER_HOST") or "").strip()
    if h:
        return h
    ep = _terraform_output_raw("rds_writer_endpoint")
    if ep:
        return ep
    raise SystemExit("DB_WRITER_HOST 미설정이며 terraform output도 실패했습니다.")


def _resolve_db_name(cli_db: Optional[str]) -> str:
    if cli_db and str(cli_db).strip():
        return str(cli_db).strip()
    return (os.getenv("DB_NAME") or "ticketing").strip()


_KUBECTL_PATH: Optional[str] = None


def _ensure_kubectl() -> Optional[str]:
    """
    Best-effort kubectl 확보.
    - 1) $KUBECTL 경로(있으면)
    - 2) PATH의 kubectl
    - 3) (Linux) dl.k8s.io 에서 /tmp/kubectl 다운로드
    """
    global _KUBECTL_PATH
    if _KUBECTL_PATH:
        return _KUBECTL_PATH

    env_path = (os.getenv("KUBECTL") or "").strip()
    if env_path:
        _KUBECTL_PATH = env_path
        return _KUBECTL_PATH

    p = which("kubectl")
    if p:
        _KUBECTL_PATH = p
        return _KUBECTL_PATH

    # 퍼블릭 파드/리눅스 환경에서 kubectl이 없을 때를 대비해 자동 다운로드(권한 문제/네트워크 없으면 실패)
    if platform.system().lower() == "linux":
        ver = (os.getenv("KUBECTL_VERSION") or "").strip() or "v1.30.7"
        arch = "amd64" if platform.machine().lower() in ("x86_64", "amd64") else "arm64"
        url = f"https://dl.k8s.io/release/{ver}/bin/linux/{arch}/kubectl"
        dst = "/tmp/kubectl"
        try:
            with urlopen(url, timeout=20) as r:
                data = r.read()
            with open(dst, "wb") as f:
                f.write(data)
            os.chmod(dst, 0o755)
            _KUBECTL_PATH = dst
            return _KUBECTL_PATH
        except Exception:
            return None

    return None


def _kubectl_cmd() -> list[str]:
    p = _ensure_kubectl()
    return [p] if p else ["kubectl"]


def _kubectl_run(args: list[str], *, timeout: float = 10.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            _kubectl_cmd() + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, "", repr(e)


def _kubectl_json(args: list[str], *, timeout: float = 10.0) -> tuple[Optional[dict], Optional[str]]:
    rc, out, err = _kubectl_run(args, timeout=timeout)
    if rc != 0:
        msg = (err or "").strip() or (out or "").strip()
        return None, (msg or f"kubectl rc={rc}")
    try:
        return json.loads(out), None
    except Exception as e:
        return None, f"json parse failed: {e!r}"


def _short_kubectl_err(msg: Optional[str]) -> str:
    """
    출력용으로 kubectl 에러를 짧게 축약한다.
    - RBAC Forbidden은 긴 문구가 붙기 쉬워, 한 줄로 정리해 “출력 포맷 붕괴”를 방지한다.
    """
    s = (msg or "").strip()
    if not s:
        return ""
    one = " ".join(s.split())
    lo = one.lower()
    if "forbidden" in lo:
        # 예: Error from server (Forbidden): ... cannot list resource ...
        return "Forbidden (RBAC)"
    if "unauthorized" in lo:
        return "Unauthorized"
    if "timed out" in lo or "timeout" in lo:
        return "Timeout"
    return one[:180]


def _kubectl_int(args: list[str], *, timeout: float = 10.0) -> Optional[int]:
    rc, out, _err = _kubectl_run(args, timeout=timeout)
    if rc != 0:
        return None
    s = (out or "").strip()
    if not s:
        return 0
    try:
        return int(s)
    except Exception:
        return None


def _k8s_snapshot_counts(namespace: str) -> dict:
    # 데이터 수집용: 스냅샷은 “최종 요약 출력 시점”의 값.
    # - 노드 수: Ready 기준(스케줄링 가능한 노드 느낌에 가깝게)
    # - 파드 수: burst Deployment의 readyReplicas(Ready 파드 수)
    ns = (namespace or "ticketing").strip() or "ticketing"

    # 퍼블릭/ops Pod에서 서비스어카운트 권한이 제한적일 수 있음.
    # 출력이 깨지는 것을 피하기 위해 기본은 best-effort로만 수집한다.
    nodes_ready: Optional[int] = None
    nodes_err: Optional[str] = None
    j_nodes, nodes_err = _kubectl_json(["get", "nodes", "-o", "json"], timeout=10.0)
    if isinstance(j_nodes, dict):
        items = j_nodes.get("items") or []
        cnt = 0
        for it in items:
            conds = (((it or {}).get("status") or {}).get("conditions") or [])
            ready_true = any(
                (c or {}).get("type") == "Ready" and (c or {}).get("status") == "True"
                for c in conds
            )
            if ready_true:
                cnt += 1
        nodes_ready = cnt

    deploys_ready: dict[str, Optional[int]] = {}
    deploys_desired: dict[str, Optional[int]] = {}
    deploys_err: Optional[str] = None
    j_deploys, deploys_err = _kubectl_json(["-n", ns, "get", "deploy", "-o", "json"], timeout=10.0)
    if isinstance(j_deploys, dict):
        by_name = {
            ((it or {}).get("metadata") or {}).get("name"): it
            for it in (j_deploys.get("items") or [])
        }
        for name in ("write-api-burst", "read-api-burst", "worker-svc-burst"):
            it = by_name.get(name) or {}
            st = (it.get("status") or {}) if isinstance(it, dict) else {}
            rr = st.get("readyReplicas")
            dr = st.get("replicas")
            deploys_ready[name] = int(rr) if isinstance(rr, int) else 0
            deploys_desired[name] = int(dr) if isinstance(dr, int) else 0

    return {
        "namespace": ns,
        "eks_nodes_ready": nodes_ready,
        "write_burst_pods_ready": deploys_ready.get("write-api-burst"),
        "read_burst_pods_ready": deploys_ready.get("read-api-burst"),
        "work_burst_pods_ready": deploys_ready.get("worker-svc-burst"),
        "write_burst_pods_desired": deploys_desired.get("write-api-burst"),
        "read_burst_pods_desired": deploys_desired.get("read-api-burst"),
        "work_burst_pods_desired": deploys_desired.get("worker-svc-burst"),
        "kubectl_nodes_error": _short_kubectl_err(nodes_err),
        "kubectl_deploys_error": _short_kubectl_err(deploys_err),
    }


def _db_connect(db_name: str):
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError:
        raise SystemExit("필요: pip install pymysql")

    return pymysql.connect(
        host=_resolve_db_writer_host(),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=db_name,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )


# ──────────────────────────────────────────────
# DB 조회
# ──────────────────────────────────────────────

def _pick_show(cur, show_id: Optional[int], concert_title: str) -> dict:
    if show_id is not None:
        cur.execute(
            """
            SELECT cs.show_id, cs.concert_id, cs.show_date,
                   cs.seat_rows, cs.seat_cols,
                   cs.total_count, cs.remain_count, cs.status,
                   c.title AS concert_title
            FROM   concert_shows cs
            INNER JOIN concerts c ON c.concert_id = cs.concert_id
            WHERE  cs.show_id = %s AND cs.show_date >= NOW()
            """,
            (show_id,),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"show_id={show_id} 없음 또는 show_date가 지났습니다.")
        return row

    cur.execute(
        """
        SELECT cs.show_id, cs.concert_id, cs.show_date,
               cs.seat_rows, cs.seat_cols,
               cs.total_count, cs.remain_count, cs.status,
               c.title AS concert_title
        FROM   concert_shows cs
        INNER JOIN concerts c ON c.concert_id = cs.concert_id
        WHERE  c.title = %s
          AND  UPPER(COALESCE(cs.status, '')) = 'OPEN'
          AND  cs.show_date >= NOW()
        ORDER  BY cs.show_date ASC
        LIMIT  1
        """,
        (concert_title,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit("조건에 맞는 회차가 없습니다. --show-id 로 지정하세요.")
    return row


def _ensure_loadtest_users_fast(
    db_name: str,
    *,
    user_base: int,
    user_count: int,
    name_prefix: str = "sqs-load-concert4-",
) -> None:
    """FK(users.user_id) 충족을 위한 유저 일괄 시드 (INSERT IGNORE executemany)."""
    n = int(user_count)
    if n <= 0:
        return
    ub = int(user_base)
    uid_hi = ub + n - 1

    conn = _db_connect(db_name)
    try:
        with conn.cursor() as cur:
            rows = [
                (uid, f"+1555{uid:010d}"[:20], "loadtest", f"{name_prefix}{uid}")
                for uid in range(ub, uid_hi + 1)
            ]
            cur.executemany(
                "INSERT IGNORE INTO users (user_id, phone, password_hash, name) "
                "VALUES (%s, %s, %s, %s)",
                rows,
            )
        print(f"[info] users ensured user_id={ub}..{uid_hi} (n={n})", file=sys.stderr)
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 좌석 매핑
# ──────────────────────────────────────────────

def _seat_for_i(i: int, *, seat_rows: int, seat_cols: int, wrap: bool) -> str:
    """i번째 요청에 대한 고유 좌석 키 반환. wrap=False이면 초과 시 빈 문자열."""
    rows = max(1, int(seat_rows))
    cols = max(1, int(seat_cols))
    cap = rows * cols
    if not wrap and i >= cap:
        return ""
    j = i % cap
    return f"{(j // cols) + 1}-{(j % cols) + 1}"


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="콘서트 10초 버스트(HTTP/WAS) 부하 v4 (개선판)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── 필수 ──
    p.add_argument(
        "-n", "--count",
        type=int, required=True,
        help=f"요청 천 단위 수.  실제 총 요청 = count × {BURST_UNIT}.  예: -n 50 → 50,000건",
    )

    # ── 타겟 ──
    p.add_argument("--show-id",       type=int,   default=None)
    p.add_argument("--concert-title", default=DEFAULT_CONCERT_TITLE)
    p.add_argument("--write-api-base", default=None, metavar="URL")
    p.add_argument("--db-name",        default=None, metavar="NAME")

    # ── 발사 ──
    p.add_argument("--duration-sec",   type=float, default=10.0,  help="버스트 발사 기간(초)")
    p.add_argument(
        "--plan",
        default="all-at-once",
        choices=["uniform", "all-at-once"],
        help="all-at-once=즉시 최대 동시 발사(스파이크 연출), uniform=duration 동안 균등 분산",
    )

    # ── 유저/좌석 ──
    p.add_argument("--user-base",   type=int, default=1,   help="가상 유저 id 시작값")
    p.add_argument("--seed-users",  action=argparse.BooleanOptionalAction, default=True)
    p.add_argument(
        "--seat-rows",
        type=int,
        default=None,
        metavar="N",
        help="좌석 row 수. 생략 시 DB concert_shows.seat_rows (0이면 500)",
    )
    p.add_argument(
        "--seat-cols",
        type=int,
        default=None,
        metavar="N",
        help="열(한 행 좌석 수). 생략 시 DB concert_shows.seat_cols (0이면 100). "
        "예전 기본 100이면 1행을 100석만 쓰고 2행으로 넘어가는 것처럼 보임",
    )
    p.add_argument(
        "--seat-wrap",
        action=argparse.BooleanOptionalAction, default=False,
        help="요청 수 > rows×cols 이면 좌석 wrap(중복).  False=초과분 스킵",
    )

    # ── HTTP ──
    p.add_argument(
        "--http-concurrency", type=int,
        default=int(os.getenv("BURST_HTTP_CONCURRENCY", "2000") or 2000),
        help="동시 in-flight HTTP 수",
    )
    p.add_argument(
        "--http-timeout", type=float,
        default=float(os.getenv("BURST_HTTP_TIMEOUT", "20") or 20),
        help="개별 HTTP 타임아웃(초)",
    )

    # ── Waiting Room ──
    p.add_argument(
        "--admit-rate", type=int,
        default=int(os.getenv("WR_ADMIT_RATE", "300000") or 300000),
        help="waiting-room MANUAL admit_rate_per_sec",
    )
    p.add_argument("--reset-wr",            action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--reset-concert-redis", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--restore-wr-auto",     action=argparse.BooleanOptionalAction, default=True)

    # ── Status poll ──
    p.add_argument(
        "--status-poll-max-sec", type=float, default=2.0,
        help="permit 획득을 위한 status 폴링 최대 시간(초)",
    )
    p.add_argument(
        "--admit-timeout-sec", type=float, default=0.0,
        help="0이면 --status-poll-max-sec 사용.  SQS 적체/오토스케일 연출은 30~600 권장",
    )
    p.add_argument("--status-interval-ms", type=int, default=50, help="status 폴링 간격(ms)")

    # ── 출력 ──
    p.add_argument("--progress-every", type=int, default=1000)

    # ── Hold 관측 ──
    p.add_argument(
        "--read-api-base",
        default=os.getenv("READ_API_BASE_URL", "").strip()
            or "http://read-api.ticketing.svc.cluster.local:5000",
        metavar="URL",
    )
    p.add_argument("--observe-holds-sec",    type=float, default=0.0)
    p.add_argument("--observe-interval-sec", type=float, default=0.5)
    p.add_argument("--observe-holds-sample", type=int,   default=10)

    return p.parse_args()


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

async def main_async() -> int:
    args = parse_args()

    # 총 요청 수 (천 단위 환산)
    n = int(args.count) * BURST_UNIT
    if n < 1:
        raise SystemExit("--count 는 1 이상 (1 = 1,000건)")

    duration   = max(0.001, float(args.duration_sec))
    conc       = max(1,     int(args.http_concurrency))
    timeout    = max(1.0,   float(args.http_timeout))
    poll_max   = float(args.admit_timeout_sec) if args.admit_timeout_sec > 0 else float(args.status_poll_max_sec)
    poll_iv    = max(1, int(args.status_interval_ms)) / 1000.0

    # write-api 베이스 URL
    if http_w is None:
        raise SystemExit("http_booking_client 모듈을 찾을 수 없습니다.")
    write_base = http_w.resolve_write_api_base(args.write_api_base)

    # ── show 선택 ──────────────────────────────
    dbn = _resolve_db_name(args.db_name)
    show_id = concert_id = remain_at_start = None
    concert_title = None
    try:
        conn = _db_connect(dbn)
        try:
            with conn.cursor() as cur:
                show          = _pick_show(cur, args.show_id, args.concert_title)
                show_id       = int(show["show_id"])
                concert_id    = int(show.get("concert_id") or 0)
                concert_title = show.get("concert_title")
                remain_at_start = int(show.get("remain_count") or 0)
        finally:
            conn.close()
    except Exception as e:
        raise SystemExit(f"show 선택 DB 조회 실패: {e!r}")

    db_sr = int(show.get("seat_rows") or 0)
    db_sc = int(show.get("seat_cols") or 0)
    seat_rows_eff = (
        int(args.seat_rows) if args.seat_rows is not None else max(1, db_sr if db_sr > 0 else 500)
    )
    seat_cols_eff = (
        int(args.seat_cols) if args.seat_cols is not None else max(1, db_sc if db_sc > 0 else 100)
    )
    print(
        f"[info] seat grid: {seat_rows_eff}x{seat_cols_eff} "
        f"(DB show seat_rows={db_sr}, seat_cols={db_sc}; "
        f"override with --seat-rows / --seat-cols)",
        file=sys.stderr,
    )

    # ── users 시드 ─────────────────────────────
    # uid 범위: user_base ~ user_base + n - 1
    if args.seed_users:
        _ensure_loadtest_users_fast(dbn, user_base=args.user_base, user_count=n)

    # ── WR / Redis 초기화 ──────────────────────
    if args.reset_wr:
        try:
            http_w.concert_waiting_room_reset(write_base, show_id, timeout=min(10.0, timeout))
        except Exception:
            pass
    if args.reset_concert_redis:
        try:
            http_w.concert_redis_reset(write_base, show_id, timeout=min(10.0, timeout))
        except Exception:
            pass
    try:
        http_w.concert_waiting_room_control(
            write_base, show_id,
            mode="MANUAL", enabled=True,
            admit_rate_per_sec=int(args.admit_rate),
            message="loadtest burst v4",
            timeout=min(10.0, timeout),
        )
    except Exception as e:
        print(f"[warn] waiting-room control 실패(계속 진행): {e!r}", file=sys.stderr)

    # ── aiohttp 준비 ───────────────────────────
    try:
        import aiohttp
    except ImportError:
        raise SystemExit("필요: pip install aiohttp")

    # ── 카운터 ─────────────────────────────────
    counters = {
        "enter_ok":             0,
        "enter_fail":           0,
        "admitted_ok":          0,
        "admitted_fail":        0,
        "commit_queued_ok":     0,
        "commit_fail":          0,
        "skipped_no_seat":      0,
        "http_429_wr_required": 0,
        "err_timeout":          0,   # ★ 추가: 타임아웃(서버 다운 징후)
        "err_500":              0,   # ★ 추가: 500 서버 에러
        "err_503":              0,   # ★ 추가: 503 과부하
        "err_sold_out":         0,   # ★ 추가: SOLD_OUT
        "err_seat_taken":       0,   # ★ 추가: SEAT_TAKEN(중복 좌석)
        "http_other_fail":      0,
    }
    enter_http      = Counter()
    commit_http     = Counter()
    commit_api_code = Counter()
    recent_commit_fail: deque[tuple] = deque(maxlen=20)
    accepted_refs:  list[str]  = []
    latencies_enter: list[float] = []  # ★ 추가: enter 응답시간
    latencies_commit: list[float] = [] # ★ 추가: commit 응답시간
    lock = asyncio.Lock()

    def _url(path: str) -> str:
        return f"{write_base}{path}"

    async def _req_json(
        session: "aiohttp.ClientSession",
        method: str,
        url: str,
        body: dict | None,
    ) -> tuple[int, dict]:
        try:
            async with session.request(
                method.upper(), url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                code = int(resp.status)
                try:
                    j = await resp.json(content_type=None)
                except Exception:
                    txt = await resp.text()
                    j = {"_parse_error": True, "_raw": txt}
                return code, j
        except asyncio.TimeoutError:
            return 0, {"_timeout": True}
        except Exception as e:
            return 0, {"_error": repr(e)}

    # ── 관측 루프 ──────────────────────────────
    observe_sec      = max(0.0, float(args.observe_holds_sec    or 0.0))
    observe_iv       = max(0.1, float(args.observe_interval_sec or 0.5))
    observe_sample_n = max(1,   int(args.observe_holds_sample   or 10))
    read_base        = str(args.read_api_base or "").strip().rstrip("/")
    observe_enabled  = (
        observe_sec > 0.0
        and concert_id and concert_id > 0
        and show_id and show_id > 0
        and bool(read_base)
    )

    async def _observe_holds_loop(session: "aiohttp.ClientSession") -> None:
        if not observe_enabled:
            return
        url      = f"{read_base}/api/read/concert/{concert_id}/booking-holds?show_id={show_id}"
        deadline = time.monotonic() + observe_sec
        last_rev = None
        max_hold = 0
        max_sample: list[str] = []
        print(f"[observe] holds url={url}", file=sys.stderr)
        while time.monotonic() < deadline:
            try:
                code_o, j_o = await _req_json(session, "GET", url, None)
            except Exception:
                await asyncio.sleep(observe_iv)
                continue
            if int(code_o or 0) != 200 or not isinstance(j_o, dict) or not j_o.get("ok"):
                await asyncio.sleep(observe_iv)
                continue
            rev = j_o.get("hold_rev")
            hc  = j_o.get("hold_count")
            try:   rev_i = int(rev or 0)
            except Exception: rev_i = 0
            try:   hc_i  = int(hc  or 0)
            except Exception: hc_i  = 0
            hs     = j_o.get("hold_seats")
            sample = [str(x) for x in (hs or [])[:observe_sample_n]]
            if hc_i > max_hold:
                max_hold   = hc_i
                max_sample = sample
            if last_rev is None or rev_i != last_rev or hc_i > 0:
                last_rev = rev_i
                print(
                    f"[observe] hold_rev={rev_i} hold_count={hc_i} sample={sample}",
                    file=sys.stderr,
                )
            await asyncio.sleep(observe_iv)
        print(f"[observe] done max_hold_count={max_hold} max_sample={max_sample}", file=sys.stderr)

    # ── 단일 요청 처리 ─────────────────────────
    async def _one(i: int, *, t0_fire: float, session: "aiohttp.ClientSession") -> None:
        # uniform 모드: duration 동안 균등 발사
        if args.plan == "uniform":
            scheduled = t0_fire + (duration * (i / max(1, n)))
            now = time.monotonic()
            if scheduled > now:
                await asyncio.sleep(scheduled - now)

        uid      = args.user_base + i   # user_base=1, i=0..n-1 → uid=1..n
        seat_key = _seat_for_i(i, seat_rows=seat_rows_eff, seat_cols=seat_cols_eff, wrap=args.seat_wrap)

        if not seat_key:
            async with lock:
                counters["skipped_no_seat"] += 1
            return

        # ── 1) enter ────────────────────────────
        t_enter = time.monotonic()
        code0, j0 = await _req_json(
            session, "POST",
            _url(f"/api/write/concerts/{show_id}/waiting-room/enter"),
            {"user_id": uid},
        )
        lat_enter = time.monotonic() - t_enter

        qref = str((j0 or {}).get("queue_ref") or "")

        async with lock:
            enter_http[int(code0)] += 1
            latencies_enter.append(lat_enter)

        if code0 != 200 or not qref:
            async with lock:
                counters["enter_fail"] += 1
                if code0 == 0 and (j0 or {}).get("_timeout"):
                    counters["err_timeout"] += 1
                elif code0 == 500:
                    counters["err_500"] += 1
                elif code0 == 503:
                    counters["err_503"] += 1
            return

        async with lock:
            counters["enter_ok"] += 1

        # ── 2) status poll ───────────────────────
        permit = ""
        deadline = time.monotonic() + poll_max
        import urllib.parse
        ref_enc  = urllib.parse.quote(str(qref).strip(), safe="")

        while poll_max > 0 and time.monotonic() < deadline:
            code1, st = await _req_json(
                session, "GET",
                _url(f"/api/write/concerts/waiting-room/status/{ref_enc}"),
                None,
            )
            if (
                isinstance(st, dict)
                and st.get("status") == "ADMITTED"
                and st.get("permit_token")
            ):
                permit = str(st["permit_token"])
                break
            await asyncio.sleep(poll_iv)

        if not permit:
            async with lock:
                counters["admitted_fail"] += 1
            return

        async with lock:
            counters["admitted_ok"] += 1

        # ── 3) commit ────────────────────────────
        t_commit = time.monotonic()
        code2, j2 = await _req_json(
            session, "POST",
            _url("/api/write/concerts/booking/commit"),
            {
                "user_id":      uid,
                "show_id":      show_id,
                "seats":        [seat_key],
                "permit_token": permit,
            },
        )
        lat_commit = time.monotonic() - t_commit

        api_code = str((j2 or {}).get("code") or "")
        ok = (code2 == 200 and bool((j2 or {}).get("ok")) and api_code == "QUEUED")

        async with lock:
            commit_http[int(code2)] += 1
            latencies_commit.append(lat_commit)
            if api_code:
                commit_api_code[api_code] += 1

        if ok:
            bref = str((j2 or {}).get("booking_ref") or "")
            async with lock:
                counters["commit_queued_ok"] += 1
                if bref:
                    accepted_refs.append(bref)
            return

        # 실패 분류
        async with lock:
            counters["commit_fail"] += 1
            if code2 == 429 and api_code == "WAITING_ROOM_REQUIRED":
                counters["http_429_wr_required"] += 1
            elif code2 == 0 and (j2 or {}).get("_timeout"):
                counters["err_timeout"] += 1
            elif code2 == 500:
                counters["err_500"] += 1
            elif code2 == 503:
                counters["err_503"] += 1
            elif api_code == "SOLD_OUT":
                counters["err_sold_out"] += 1
            elif api_code == "SEAT_TAKEN":
                counters["err_seat_taken"] += 1
            else:
                counters["http_other_fail"] += 1

            try:
                msg = str((j2 or {}).get("message") or (j2 or {}).get("detail") or "")
            except Exception:
                msg = ""
            recent_commit_fail.append((int(code2), api_code, msg[:120]))

    # ──────────────────────────────────────────
    # ★ 개선: Producer-Consumer 패턴
    #   - 3만개 Task를 한 번에 생성하지 않고 Queue로 흘려보냄
    #   - 메모리: O(conc) 수준으로 제한
    # ──────────────────────────────────────────
    t_all0  = time.monotonic()
    t_fire0 = None

    try:
        connector = aiohttp.TCPConnector(limit=conc, limit_per_host=conc)
        async with aiohttp.ClientSession(connector=connector) as session:

            observe_task = (
                asyncio.create_task(_observe_holds_loop(session))
                if observe_enabled else None
            )

            t_fire0 = time.monotonic()
            done    = 0
            queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=conc * 2)

            async def _producer() -> None:
                for idx in range(n):
                    await queue.put(idx)
                for _ in range(conc):
                    await queue.put(None)  # sentinel: worker 종료 신호

            async def _worker() -> None:
                nonlocal done
                while True:
                    idx = await queue.get()
                    if idx is None:
                        return
                    await _one(idx, t0_fire=t_fire0, session=session)
                    async with lock:
                        done += 1
                        if done % args.progress_every == 0 or done == n:
                            elapsed  = max(0.001, time.monotonic() - t_fire0)
                            qps      = done / elapsed
                            top_enter  = enter_http.most_common(3)
                            top_commit = commit_api_code.most_common(3)
                            # 실시간 로스율
                            loss_pct = (
                                (done - counters["commit_queued_ok"]) / done * 100
                                if done > 0 else 0.0
                            )
                            print(
                                f"progress: {done}/{n}  qps={qps:.1f}  "
                                f"enter_ok={counters['enter_ok']}  enter_fail={counters['enter_fail']}  "
                                f"admit_ok={counters['admitted_ok']}  admit_fail={counters['admitted_fail']}  "
                                f"queued_ok={counters['commit_queued_ok']}  commit_fail={counters['commit_fail']}  "
                                f"loss={loss_pct:.1f}%  "
                                f"timeout={counters['err_timeout']}  "
                                f"500={counters['err_500']}  503={counters['err_503']}  "
                                f"sold_out={counters['err_sold_out']}  seat_taken={counters['err_seat_taken']}  "
                                f"wr429={counters['http_429_wr_required']}  "
                                f"enter_http={top_enter}  commit_code={top_commit}",
                                file=sys.stderr,
                            )

            await asyncio.gather(
                _producer(),
                *[_worker() for _ in range(conc)],
            )

    finally:
        if "observe_task" in locals() and observe_task:
            observe_task.cancel()
        if args.restore_wr_auto:
            try:
                http_w.concert_waiting_room_control(
                    write_base, show_id,
                    mode="AUTO", enabled=True,
                    admit_rate_per_sec=None, message="",
                    timeout=min(10.0, timeout),
                )
            except Exception:
                pass

    # ──────────────────────────────────────────
    # ★ 통계 계산
    # ──────────────────────────────────────────
    total_sec = time.monotonic() - t_all0
    fire_sec  = max(0.001, time.monotonic() - (t_fire0 or t_all0))

    def _percentiles(lst: list[float]) -> dict:
        if not lst:
            return {"p50": None, "p95": None, "p99": None, "max": None, "avg": None}
        s = sorted(lst)
        sz = len(s)
        return {
            "p50": round(s[sz // 2],              4),
            "p95": round(s[int(sz * 0.95)],       4),
            "p99": round(s[int(sz * 0.99)],       4),
            "max": round(s[-1],                    4),
            "avg": round(sum(s) / sz,              4),
            "n":   sz,
        }

    total_attempted = n
    total_queued    = counters["commit_queued_ok"]
    loss_rate_pct   = round((total_attempted - total_queued) / max(1, total_attempted) * 100, 2)

    summary = {
        "show_id":        show_id,
        "concert_id":     concert_id,
        "concert_title":  concert_title,
        "remain_at_start": remain_at_start,

        # ── 발사 조건 ──
        "total_requested": total_attempted,
        "count_args":      {"count_input": args.count, "unit": BURST_UNIT},
        "duration_sec":    duration,
        "plan":            args.plan,
        "http_concurrency": conc,
        "http_timeout_sec": timeout,
        "seat_grid":       f"{seat_rows_eff}x{seat_cols_eff}",
        "seat_grid_db":    {"seat_rows": db_sr, "seat_cols": db_sc},
        "seat_grid_cli":   {"seat_rows": args.seat_rows, "seat_cols": args.seat_cols},
        "seat_wrap":       args.seat_wrap,

        # ── 핵심 결과 (발표용) ──
        "result_summary": {
            "total_requested":   total_attempted,
            "commit_queued_ok":  total_queued,
            "loss_count":        total_attempted - total_queued,
            "loss_rate_pct":     loss_rate_pct,           # ★ 로스율
            "note_queued":       "QUEUED = SQS 접수 성공. 최종 DB 반영은 worker 처리 후 확인 필요.",
        },

        # ── 단계별 카운터 ──
        "stats": counters,

        # ── HTTP 상태 분포 ──
        "enter_http_status":  dict(enter_http),
        "commit_http_status": dict(commit_http),
        "commit_api_code":    dict(commit_api_code),

        # ── ★ 응답 시간 (P50/P95/P99) ──
        "latency_enter_sec":  _percentiles(latencies_enter),
        "latency_commit_sec": _percentiles(latencies_commit),

        # ── 실패 샘플 ──
        "recent_commit_fail_sample": list(recent_commit_fail),

        # ── accepted refs ──
        "accepted_refs_count":  len(accepted_refs),
        "accepted_refs_sample": accepted_refs[:5],

        # ── 시간 ──
        "fire_elapsed_sec":  round(fire_sec,  3),
        "total_elapsed_sec": round(total_sec, 3),
        "fire_qps":          round(total_attempted / fire_sec, 2),

        # ── WR 설정 ──
        "wr_admit_rate_per_sec":       args.admit_rate,
        "status_poll_max_sec":         args.status_poll_max_sec,
        "admit_timeout_sec":           args.admit_timeout_sec or None,
        "permit_wait_max_sec_effective": poll_max,
        "status_interval_ms":          args.status_interval_ms,
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # ── 발표용 요약 한 줄 출력 ──────────────────
    print(
        f"\n{'='*60}\n"
        f"  총 요청:    {total_attempted:,}건\n"
        f"  SQS 접수:   {total_queued:,}건\n"
        f"  로스:       {total_attempted - total_queued:,}건  ({loss_rate_pct}%)\n"
        f"  처리 시간:  {fire_sec:.2f}초  ({total_attempted/fire_sec:.0f} RPS)\n"
        f"  enter P99:  {_percentiles(latencies_enter)['p99']}s\n"
        f"  commit P99: {_percentiles(latencies_commit)['p99']}s\n"
        f"  타임아웃:   {counters['err_timeout']}건\n"
        f"  500에러:    {counters['err_500']}건\n"
        f"  503에러:    {counters['err_503']}건\n"
        f"  SOLD_OUT:   {counters['err_sold_out']}건\n"
        f"  SEAT_TAKEN: {counters['err_seat_taken']}건\n"
        f"{'='*60}\n"
        f"{'='*60}",
        file=sys.stderr,
    )

    return 0


def main() -> None:
    try:
        code = asyncio.run(main_async())
    except KeyboardInterrupt:
        raise SystemExit(130)
    # 마지막 출력까지 끝난 뒤, "호스트에서 kubectl로 찍는 스냅샷"을 실행시키기 위한 트리거.
    #
    # 이 파이썬이 tools-once Pod 안에서 실행되는 경우, Pod SA 권한으로는 nodes/deploy가 막힐 수 있다.
    # 그래서 "호스트 셸"이 이 라인을 감지해서 로컬 스크립트를 실행하도록 한다.
    # - 파드 내부에서는 이 마커를 출력만 한다(실행은 호스트가 함).
    post = (os.getenv("POST_HOST_SNAPSHOT_SH") or "").strip()
    if post:
        # stderr로 보내서 요약 출력(대부분 stderr) 흐름과 같이 보이게 한다.
        print(f"HOST_SNAPSHOT_CMD: {post}", file=sys.stderr, flush=True)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
