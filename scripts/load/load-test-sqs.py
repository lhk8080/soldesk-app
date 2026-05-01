#!/usr/bin/env python3
"""KEDA vs HPA 비교용 부하 생성기 — boto3 + ThreadPool

사용:
  python3 load-test-sqs.py 1000 spike      # 1000건을 50 thread로 한방에
  python3 load-test-sqs.py 1500 sustained  # 5 RPS로 1500건 (5분)

monitoring EC2에서 실행 (boto3가 설치되어 있고 SQS와 같은 region/VPC라 latency 짧음)
"""
import boto3
import json
import os
import sys
import time
import concurrent.futures

COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
PATTERN = sys.argv[2] if len(sys.argv) > 2 else "spike"

SQS_URL = os.environ.get("SQS_QUEUE_URL")
if not SQS_URL:
    print("ERROR: SQS_QUEUE_URL env 변수가 필요합니다.", file=sys.stderr)
    print("  예: export SQS_QUEUE_URL=$(terraform -chdir=terraform output -raw sqs_queue_url)",
          file=sys.stderr)
    sys.exit(1)
EVENT_ID = "a1b2c3d4-0001-0001-0001-000000000001"
TS = int(time.time() * 1_000_000_000)

sqs = boto3.client("sqs", region_name="ap-northeast-2")


def make_entry(i: int) -> dict:
    return {
        "Id": str(i),
        "MessageGroupId": f"loadtest-g{i % 50}",
        "MessageDeduplicationId": f"loadtest-{TS}-{i}",
        "MessageBody": json.dumps({
            "reservationId": f"loadtest-{TS}-{i}",
            "userId": "loadtest@example.com",
            "eventId": EVENT_ID,
            "seatIds": [f"fake-seat-{i}"],
            "expiresAt": "2099-01-01T00:00:00Z",
            "lockKeys": [],
        }),
    }


def send_batch(batch: list) -> dict:
    return sqs.send_message_batch(QueueUrl=SQS_URL, Entries=batch)


# 10건씩 batch 묶기
batches = []
for start in range(1, COUNT + 1, 10):
    end = min(start + 10, COUNT + 1)
    batches.append([make_entry(j) for j in range(start, end)])

t0 = time.time()
print(f"[load] start: count={COUNT} batches={len(batches)} pattern={PATTERN}")

ok = 0
fail = 0

if PATTERN == "spike":
    # 50 thread로 모든 batch 동시 발사
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
        futures = [ex.submit(send_batch, b) for b in batches]
        for f in concurrent.futures.as_completed(futures):
            try:
                resp = f.result()
                ok += len(resp.get("Successful", []))
                fail += len(resp.get("Failed", []))
            except Exception as e:
                fail += 10
                print(f"[load] batch error: {e}", file=sys.stderr)

elif PATTERN == "sustained":
    # 1 batch (10건) per 2초 → 5 RPS
    for b in batches:
        try:
            resp = send_batch(b)
            ok += len(resp.get("Successful", []))
            fail += len(resp.get("Failed", []))
        except Exception as e:
            fail += 10
            print(f"[load] batch error: {e}", file=sys.stderr)
        time.sleep(2)
else:
    print(f"unknown pattern: {PATTERN}", file=sys.stderr)
    sys.exit(1)

elapsed = time.time() - t0
print(f"[load] done: elapsed={elapsed:.2f}s ok={ok} fail={fail} rate={ok / elapsed:.0f}/s")
