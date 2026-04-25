"""
SQS 부하 스크립트 공통: 큐 가시+인플라이트+지연 메시지가 비워질 때까지 대기(근사치).
워커가 ACK 하기 전 인플라이트가 잡히므로, UI/다음 예매 가능 시점에 가깝게 맞추려면 이 대기 후를 본다.
"""
from __future__ import annotations

import time
from typing import Any


def get_queue_depth_triplet(sqs_client: Any, queue_url: str) -> tuple[int, int, int]:
    resp = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ],
    )
    a = resp.get("Attributes") or {}

    def _i(key: str) -> int:
        try:
            return int(str(a.get(key) or "0").strip() or "0", 10)
        except ValueError:
            return 0

    return (
        _i("ApproximateNumberOfMessages"),
        _i("ApproximateNumberOfMessagesNotVisible"),
        _i("ApproximateNumberOfMessagesDelayed"),
    )


def wait_sqs_queue_idle(
    sqs_client: Any,
    queue_url: str,
    *,
    timeout_sec: float = 900.0,
    poll_interval_sec: float = 2.0,
    stable_rounds: int = 2,
) -> dict:
    """
    가시+인플라이트+지연 합이 0인 상태가 stable_rounds 회 연속(폴링 간격마다)이면 종료.
    SQS 근사치·지연 반영 때문에 2회 연속 권장.
    """
    t_start = time.monotonic()
    deadline = t_start + max(1.0, float(timeout_sec))
    interval = max(0.5, float(poll_interval_sec))
    consecutive_zero = 0
    last_vis = last_invis = last_delayed = 0
    timed_out = False

    while time.monotonic() < deadline:
        vis, invis, delayed = get_queue_depth_triplet(sqs_client, queue_url)
        last_vis, last_invis, last_delayed = vis, invis, delayed
        total = vis + invis + delayed
        if total == 0:
            consecutive_zero += 1
            if consecutive_zero >= stable_rounds:
                break
        else:
            consecutive_zero = 0
        time.sleep(interval)
    else:
        timed_out = True
        last_vis, last_invis, last_delayed = get_queue_depth_triplet(sqs_client, queue_url)

    elapsed = time.monotonic() - t_start
    return {
        "큐_소진_대기_초": round(elapsed, 3),
        "큐_대기_타임아웃": timed_out,
        "큐_종료_가시": last_vis,
        "큐_종료_인플라이트": last_invis,
        "큐_종료_지연": last_delayed,
    }
