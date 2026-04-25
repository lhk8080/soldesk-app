"""
콘서트3 검증용 Locust 시나리오 (최소 로직)

목표
- N명 동시 접속 증가 → 대기열 생성/ADMITTED 전환 관측
- ADMITTED 된 일부 유저만 commit(write) → SQS/worker 백그라운드 처리(hold/confirm) 관측
- 최종 booking status OK/FAIL 비율로 DB write 실패율 관측

중요: 이 파일은 "연출용 리셋/좌석 확보/홀드 관측"을 하지 않습니다.
      서버가 설계대로 흘러가는지(대기열→permit→write→최종상태)만 봅니다.
"""

from __future__ import annotations

import os
import random
import time
import uuid
from dataclasses import dataclass

from locust import HttpUser, between, events, task


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw, 10)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in ("1", "true", "yes", "y", "on")


def _validate_host(host: str | None) -> str:
    h = (host or "").strip()
    if not h:
        raise RuntimeError(
            "Locust host is missing. Pass --host/-H (e.g. -H http://ticketing-write.ticketing.svc.cluster.local)"
        )
    # 흔한 실수: 문서/메시지의 placeholder를 그대로 복사
    if "<" in h or ">" in h:
        raise RuntimeError(f"Locust host looks like a placeholder: {h!r}. Replace it with a real URL via --host/-H.")
    if not (h.startswith("http://") or h.startswith("https://")):
        raise RuntimeError(f"Locust host must include scheme (http/https), got: {h!r}")
    return h


def _http_timeout() -> tuple[float, float]:
    # requests timeout tuple: (connect, read)
    connect = max(0.1, _env_float("LOCUST_HTTP_CONNECT_TIMEOUT_SEC", 3.0))
    read = max(0.1, _env_float("LOCUST_HTTP_READ_TIMEOUT_SEC", 30.0))
    return connect, read


@dataclass
class RunConfig:
    show_id: int
    user_base: int
    seat_rows: int
    seat_cols: int
    commit_fraction: float
    admit_poll_interval_sec: float
    booking_poll_interval_sec: float
    admit_timeout_sec: float
    booking_timeout_sec: float
    add_trace_id: bool


def _load_cfg() -> RunConfig:
    show_id = _env_int("CONCERT_SHOW_ID", 0)
    if show_id <= 0:
        raise RuntimeError("CONCERT_SHOW_ID env is required (e.g. 8)")
    return RunConfig(
        show_id=show_id,
        user_base=_env_int("USER_BASE", 1),
        seat_rows=max(1, _env_int("SEAT_ROWS", 200)),
        seat_cols=max(1, _env_int("SEAT_COLS", 250)),
        commit_fraction=min(1.0, max(0.0, _env_float("COMMIT_FRACTION", 0.05))),
        admit_poll_interval_sec=max(0.1, _env_float("ADMIT_POLL_INTERVAL_SEC", 0.4)),
        booking_poll_interval_sec=max(0.1, _env_float("BOOKING_POLL_INTERVAL_SEC", 0.4)),
        admit_timeout_sec=max(1.0, _env_float("ADMIT_TIMEOUT_SEC", 600.0)),
        booking_timeout_sec=max(1.0, _env_float("BOOKING_TIMEOUT_SEC", 600.0)),
        add_trace_id=_env_bool("ADD_TRACE_ID", True),
    )


def _seat_key(rows: int, cols: int, salt: int) -> str:
    # 충돌을 줄이기 위해 사용자별 salt 기반으로 분산. (서버가 좌석 규칙을 엄격히 제한한다면 env로 rows/cols를 맞추면 됨)
    r = (salt % rows) + 1
    c = ((salt // rows) % cols) + 1
    return f"{r}-{c}"


def _fire_counter(name: str, ok: bool, start_time: float, message: str | None = None) -> None:
    # Locust 통계에 "카운터성 이벤트"를 태우기 위한 가짜 request 이벤트.
    # response_time은 0에 가깝게(논리 이벤트)로 기록한다.
    elapsed_ms = max(0.0, (time.perf_counter() - start_time) * 1000.0)
    if ok:
        events.request.fire(
            request_type="CHECK",
            name=name,
            response_time=elapsed_ms,
            response_length=0,
            exception=None,
        )
    else:
        events.request.fire(
            request_type="CHECK",
            name=name,
            response_time=elapsed_ms,
            response_length=0,
            exception=RuntimeError(message or "failed"),
        )


class Concert3User(HttpUser):
    # 너무 빠르게만 때리지 않도록 기본 think time. (RPS는 Locust spawn rate / wait_time로 조절)
    wait_time = between(0.05, 0.3)

    def on_start(self) -> None:
        # Locust의 HttpUser.host는 self.client의 base host로 사용된다.
        # 여기서 self.host를 덮어쓰면 요청 URL이 깨질 수 있으므로, environment.host만 검증한다.
        env_host = getattr(self.environment, "host", None) if self.environment else None
        if not env_host:
            env_host = (os.getenv("LOCUST_HOST") or "").strip() or None
        _validate_host(env_host)
        self.cfg = _load_cfg()
        # Locust는 runner가 user의 user_id를 자동으로 주지 않으므로, 각 유저 인스턴스마다 고유값 생성
        self.user_id = self.cfg.user_base + random.randint(0, 2_000_000_000)
        self.queue_ref: str | None = None
        self.permit_token: str | None = None
        self.did_commit = False
        self.booking_ref: str | None = None

        self._common_headers = {}
        if self.cfg.add_trace_id:
            self._common_headers["X-Trace-Id"] = str(uuid.uuid4())

        # 1) enter: "대기열 모달"을 띄우는 트래픽 증가
        try:
            with self.client.post(
                f"/api/write/concerts/{self.cfg.show_id}/waiting-room/enter",
                json={"user_id": int(self.user_id)},
                headers=self._common_headers,
                name="WR enter",
                timeout=_http_timeout(),
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"http={resp.status_code}")
                    return
                try:
                    j = resp.json()
                except Exception:
                    resp.failure("bad_json")
                    return
                qref = str((j or {}).get("queue_ref") or "")
                if not ((j or {}).get("ok") is True and qref):
                    resp.failure(f"bad_body: {j}")
                    return
                self.queue_ref = qref
                resp.success()
        except Exception as e:
            # catch_response 컨텍스트 밖에서 터지면 통계가 안 남을 수 있어, 명시적으로 남긴다.
            events.request.fire(
                request_type="CHECK",
                name="WR enter (client_error)",
                response_time=0.0,
                response_length=0,
                exception=e,
            )

    @task
    def flow(self) -> None:
        # enter 실패한 유저는 더 진행하지 않음
        if not self.queue_ref:
            self.environment.runner.quit() if self.environment.runner else None
            return

        # 1) status poll: ADMITTED 확인(=모달이 대기 상태인지/통과되는지)
        if not self.permit_token:
            t0 = time.perf_counter()
            deadline = time.monotonic() + self.cfg.admit_timeout_sec
            while time.monotonic() < deadline:
                with self.client.get(
                    f"/api/write/concerts/waiting-room/status/{self.queue_ref}",
                    headers=self._common_headers,
                    name="WR status",
                    timeout=_http_timeout(),
                    catch_response=True,
                ) as resp:
                    if resp.status_code != 200:
                        # transient를 감안해 계속 폴링하되, 실패율은 통계로 남김
                        resp.failure(f"http={resp.status_code}")
                    else:
                        try:
                            st = resp.json()
                        except Exception:
                            resp.failure("bad_json")
                        else:
                            status = (st or {}).get("status")
                            if status == "ADMITTED" and (st or {}).get("permit_token"):
                                self.permit_token = str((st or {}).get("permit_token"))
                                resp.success()
                                _fire_counter("WR admitted", True, t0)
                                break
                            # WAITING 등은 정상
                            resp.success()
                if self.permit_token:
                    break
                time.sleep(self.cfg.admit_poll_interval_sec)

            if not self.permit_token:
                _fire_counter("WR admitted", False, t0, "timeout_no_permit")
                # 이 유저는 여기서 종료(대기열은 들어갔고, admitted까지 못 갔다는 관측이 목적)
                self.stop(True)
                return

        # 2) 일부 유저만 commit(write) 수행 → SQS/worker 백그라운드 처리량 생성
        if not self.did_commit:
            self.did_commit = True
            if random.random() > self.cfg.commit_fraction:
                # admitted까지는 관측했지만 write는 하지 않는 유저
                self.stop(True)
                return

            salt = (int(self.user_id) ^ (self.cfg.show_id * 1_000_003)) & 0x7FFFFFFF
            seat = _seat_key(self.cfg.seat_rows, self.cfg.seat_cols, salt)
            with self.client.post(
                "/api/write/concerts/booking/commit",
                json={
                    "user_id": int(self.user_id),
                    "show_id": int(self.cfg.show_id),
                    "seats": [seat],
                    "permit_token": str(self.permit_token),
                },
                headers=self._common_headers,
                name="Commit (QUEUED)",
                timeout=_http_timeout(),
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"http={resp.status_code}")
                    self.stop(True)
                    return
                try:
                    j = resp.json()
                except Exception:
                    resp.failure("bad_json")
                    self.stop(True)
                    return
                code = str((j or {}).get("code") or "")
                if (j or {}).get("ok") is True and code == "QUEUED" and (j or {}).get("booking_ref"):
                    self.booking_ref = str((j or {}).get("booking_ref"))
                    resp.success()
                else:
                    resp.failure(f"api_code={code or 'NONE'} body={j}")
                    self.stop(True)
                    return

        # 3) booking status poll: 최종 OK/FAIL로 DB write 성공/실패율 확인
        if self.booking_ref:
            t0 = time.perf_counter()
            deadline = time.monotonic() + self.cfg.booking_timeout_sec
            last_status = ""
            last_code = ""
            while time.monotonic() < deadline:
                with self.client.get(
                    f"/api/write/concerts/booking/status/{self.booking_ref}",
                    headers=self._common_headers,
                    name="Booking status",
                    timeout=_http_timeout(),
                    catch_response=True,
                ) as resp:
                    if resp.status_code != 200:
                        resp.failure(f"http={resp.status_code}")
                    else:
                        try:
                            j = resp.json()
                        except Exception:
                            resp.failure("bad_json")
                        else:
                            last_status = str((j or {}).get("status") or "")
                            last_code = str((j or {}).get("code") or "")
                            # 터미널 조건(서버 구현에 맞춰 유연하게)
                            if (j or {}).get("ok") is True and last_code == "OK":
                                resp.success()
                                _fire_counter("Booking terminal OK", True, t0)
                                self.stop(True)
                                return
                            if (j or {}).get("ok") is False and last_code:
                                resp.success()
                                _fire_counter(f"Booking terminal FAIL/{last_code}", False, t0, last_code)
                                self.stop(True)
                                return
                            if last_status and last_status not in ("PROCESSING", "QUEUED"):
                                # UNKNOWN_OR_EXPIRED, INVALID_REF, TIMEOUT 같은 값도 분리해서 기록
                                resp.success()
                                _fire_counter(f"Booking terminal {last_status}", False, t0, last_status)
                                self.stop(True)
                                return
                            resp.success()
                time.sleep(self.cfg.booking_poll_interval_sec)

            _fire_counter(f"Booking terminal TIMEOUT/{last_status or last_code or 'NO_STATUS'}", False, t0, "timeout")
            self.stop(True)

