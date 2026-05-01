#!/usr/bin/env bash
# Worker-svc replica + SQS 큐 길이 폴링
# 5초마다 CSV 한 줄 출력 -> 파일로 redirect해서 그래프용
#
# 사용:
#   ./poll-replicas.sh > replicas.csv
#   (Ctrl+C로 종료)
set -euo pipefail

NS="ticketing"
DEPLOY="worker-svc"
INTERVAL=${1:-5}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/terraform"

# SQS URL / region — terraform output 으로만 얻는다 (계정 ID 하드코딩 금지).
# 수동 override 가 필요하면 SQS_QUEUE_URL env 변수로 주입.
SQS_URL=""
REGION=""
if [[ -d "$TF_DIR" ]]; then
  SQS_URL=$(terraform -chdir="$TF_DIR" output -raw sqs_queue_url 2>/dev/null || true)
  REGION=$(terraform -chdir="$TF_DIR" output -raw aws_region 2>/dev/null || true)
fi
SQS_URL="${SQS_QUEUE_URL:-$SQS_URL}"
REGION="${REGION:-ap-northeast-2}"
if [[ -z "$SQS_URL" ]]; then
  echo "ERROR: SQS queue URL 을 알 수 없음 — terraform output 또는 SQS_QUEUE_URL env 필요" >&2
  exit 1
fi

echo "ts,replicas,ready,queue_visible,queue_in_flight"

while true; do
  TS=$(date +%s)
  R=$(kubectl get deploy -n "$NS" "$DEPLOY" -o jsonpath='{.status.replicas}' 2>/dev/null || echo "")
  READY=$(kubectl get deploy -n "$NS" "$DEPLOY" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "")
  QATTR=$(aws sqs get-queue-attributes \
    --queue-url "$SQS_URL" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
    --region "$REGION" \
    --query 'Attributes.[ApproximateNumberOfMessages, ApproximateNumberOfMessagesNotVisible]' \
    --output text 2>/dev/null || echo "0	0")
  QV=$(echo "$QATTR" | cut -f1)
  QIF=$(echo "$QATTR" | cut -f2)
  echo "${TS},${R:-0},${READY:-0},${QV:-0},${QIF:-0}"
  sleep "$INTERVAL"
done
