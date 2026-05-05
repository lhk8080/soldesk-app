#!/usr/bin/env python3
"""
SQS FIFO 부하 테스트 — N건 send_message_batch (건당 API 최대 10건).

  send_message_batch 의 각 Entry 는 **별도의 SQS 메시지**이다.
  (한 메시지 안에 예매 N건을 JSON 배열로 넣는 구조가 아니므로, 워커·FIFO 그룹 분산과 혼동하지 말 것.)

  pip install boto3   # 없을 때만 (ticketing-was requirements.txt 에도 있음)

실행 cwd 무관 — 이 파일이 저장소의 scripts/ 아래 있다고 가정하고,
  SQS_QUEUE_URL / --queue-url 이 없으면 terraform output sqs_queue_url 을 자동 조회한다.

예 (terraform/ 디렉터리에서):
  python3 ../scripts/sqs_load_send.py -n 10000

FIFO는 MessageGroupId 당 순차 처리이므로, --groups 로 그룹을 나눠 워커 병렬도를 올립니다.

주의:
  - 워커가 돌면 메시지당 DB 조회 + Redis (없는 schedule_id → NOT_FOUND 후 ACK).
  - 큐에만 쌓이는 속도만 재려면 워커 레플리카 0 등으로 소비를 잠시 끊을 것.

JSON: 전송_소요_초(배치 전송만), 큐_소진_대기_초(기본 대기), 전체_소요_초·소요_초(전송+큐 소진).
  큐 대기 끄기: --no-wait-sqs-queue
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("boto3 가 필요합니다: pip install boto3", file=sys.stderr)
    sys.exit(1)

from sqs_load_common import wait_sqs_queue_idle


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SQS FIFO load: batched SendMessage")
    p.add_argument("-n", "--count", type=int, default=1000, help="총 메시지 수 (기본 1000)")
    p.add_argument(
        "--queue-url",
        default=os.getenv("SQS_QUEUE_URL", "").strip(),
        help="큐 URL (미지정 시 SQS_QUEUE_URL → 이 스크립트 기준 저장소 terraform output)",
    )
    p.add_argument(
        "--region",
        default=os.getenv("AWS_DEFAULT_REGION", os.getenv("AWS_REGION", "ap-northeast-2")),
        help="리전",
    )
    p.add_argument(
        "--groups",
        type=int,
        default=50,
        metavar="G",
        help="FIFO MessageGroupId 개수 (부하를 그룹별로 분산, 기본 50)",
    )
    p.add_argument(
        "--no-wait-sqs-queue",
        action="store_true",
        help="전송 후 큐(가시+인플라이트+지연)가 비워질 때까지 대기하지 않음",
    )
    p.add_argument(
        "--wait-queue-timeout-sec",
        type=float,
        default=900.0,
        metavar="SEC",
        help="큐 소진 대기 최대(초), 기본 900",
    )
    p.add_argument(
        "--wait-queue-poll-sec",
        type=float,
        default=2.0,
        metavar="SEC",
        help="큐 깊이 폴링 간격(초), 기본 2",
    )
    p.add_argument(
        "--pre-delay-sec",
        type=float,
        default=0.0,
        metavar="SEC",
        help="전송 시작 전 대기 시간(초). 데모 영상 녹화 시작 후 카운트다운용",
    )
    return p.parse_args()


def _queue_url_from_terraform() -> str:
    """scripts/ 기준 상위 디렉터리 = 저장소 루트, terraform output -raw sqs_queue_url."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    tf_dir = repo_root / "terraform"
    if not tf_dir.is_dir():
        return ""
    try:
        # -chdir 는 버전별로 "-chdir=path" 만 허용하는 경우가 있어 cwd 로 통일
        proc = subprocess.run(
            ["terraform", "output", "-raw", "sqs_queue_url"],
            cwd=str(tf_dir),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError:
        print("terraform 명령을 찾을 수 없습니다 (PATH 확인).", file=sys.stderr)
        return ""
    except subprocess.TimeoutExpired:
        print("terraform output 시간 초과.", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if err:
            print(f"terraform output 실패: {err}", file=sys.stderr)
        return ""
    return proc.stdout.strip()


def message_body(i: int) -> dict:
    # worker: 스케줄 없음 → NOT_FOUND → store_result → delete (ACK)
    return {
        "booking_type": "theater",
        "booking_ref": str(uuid.uuid4()),
        "user_id": 1,
        "schedule_id": 999_999_999,
        "seats": ["1-1"],
        "load_seq": i,
    }


def main() -> None:
    args = parse_args()
    if args.count < 1:
        print("--count 는 1 이상", file=sys.stderr)
        sys.exit(1)
    if not args.queue_url:
        args.queue_url = _queue_url_from_terraform()
    if not args.queue_url:
        print(
            "큐 URL 필요: --queue-url, SQS_QUEUE_URL, 또는 "
            "이 스크립트가 <repo>/scripts/sqs_load_send.py 이고 terraform output sqs_queue_url 사용 가능해야 함.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.groups < 1:
        args.groups = 1

    client = boto3.client("sqs", region_name=args.region)

    # 데모 녹화용 카운트다운: --pre-delay-sec 만큼 stderr 로 매초 카운트
    if args.pre_delay_sec and args.pre_delay_sec > 0:
        remaining = int(args.pre_delay_sec)
        print(f"전송까지 {remaining}초 대기...", file=sys.stderr, flush=True)
        while remaining > 0:
            print(f"  T-{remaining}s", file=sys.stderr, flush=True)
            time.sleep(1)
            remaining -= 1
        print("부하 시작!", file=sys.stderr, flush=True)
    groups = max(1, int(args.groups))
    sent = 0
    errors = 0
    t_script0 = time.monotonic()
    t_tx_start = time.monotonic()

    for batch_start in range(0, args.count, 10):
        batch_len = min(10, args.count - batch_start)
        entries = []
        for j in range(batch_len):
            i = batch_start + j
            body = json.dumps(message_body(i), sort_keys=True)
            gid = f"load-{i % groups}"
            entries.append(
                {
                    "Id": str(j),
                    "MessageBody": body,
                    "MessageGroupId": gid,
                }
            )
        try:
            resp = client.send_message_batch(QueueUrl=args.queue_url, Entries=entries)
        except ClientError as e:
            print(f"send_message_batch 실패: {e}", file=sys.stderr)
            sys.exit(1)
        for f in resp.get("Failed", []) or []:
            errors += 1
            print(f"Failed entry: {f}", file=sys.stderr)
        sent += len(resp.get("Successful", []) or [])

    t_tx_done = time.monotonic()
    tx_sec = round(t_tx_done - t_tx_start, 3)
    rate = sent / tx_sec if tx_sec > 0 else 0

    if not args.no_wait_sqs_queue:
        qinfo = wait_sqs_queue_idle(
            client,
            args.queue_url,
            timeout_sec=float(args.wait_queue_timeout_sec),
            poll_interval_sec=float(args.wait_queue_poll_sec),
        )
    else:
        qinfo = {
            "큐_소진_대기_초": 0.0,
            "큐_대기_타임아웃": False,
            "큐_종료_가시": None,
            "큐_종료_인플라이트": None,
            "큐_종료_지연": None,
        }

    t_final = time.monotonic()
    prep_sec = round(t_tx_start - t_script0, 3)
    total_sec = round(t_final - t_script0, 3)

    def _fmt_dur(sec):
        if sec is None:
            return None
        try:
            s = float(sec)
        except (TypeError, ValueError):
            return None
        if s < 1:
            return f"{s * 1000:.0f}ms"
        if s < 60:
            return f"{s:.2f}s"
        m, r = divmod(s, 60)
        return f"{int(m)}m {r:.1f}s"

    drain_sec = qinfo.get("큐_소진_대기_초")

    print(
        json.dumps(
            {
                "전송_성공": sent,
                "전송_실패": errors,
                "준비_소요_초": prep_sec,
                "전송_소요_초": tx_sec,
                "폴링_소요_초": 0.0,
                **qinfo,
                "전체_소요_초": total_sec,
                "소요_초": total_sec,
                "초당_메시지": round(rate, 1),
                "FIFO_그룹_수": groups,
                "테스트후_잔여좌석": None,
                # ── 사람이 읽기 좋은 단위 표기 ──
                "_단위": {
                    "준비": _fmt_dur(prep_sec),
                    "전송": _fmt_dur(tx_sec),
                    "큐_소진_대기": _fmt_dur(drain_sec),
                    "전체": _fmt_dur(total_sec),
                    "처리량": f"{rate:.1f} msg/s",
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(
        f"\n=== 요약 ===\n"
        f"전송   : {sent}건 ({_fmt_dur(tx_sec)}) → {rate:.1f} msg/s\n"
        f"큐 소진: {_fmt_dur(drain_sec)}\n"
        f"전체   : {_fmt_dur(total_sec)}",
        file=sys.stderr,
    )
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
