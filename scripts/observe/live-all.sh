#!/usr/bin/env bash
# tmux 4분할: live-worker / worker logs / pod+hpa / poll-replicas csv
set -euo pipefail

INFRA_DIR="${INFRA_DIR:-/home/lhk64/soldesk-infra/infra}"
APP_DIR="${APP_DIR:-/home/lhk64/soldesk-app}"
SESSION="${SESSION:-obs}"

export SQS_QUEUE_URL="${SQS_QUEUE_URL:-$(terraform -chdir="$INFRA_DIR" output -raw sqs_reservation_url)}"
export AWS_REGION="${AWS_REGION:-ap-northeast-2}"

if [[ -z "$SQS_QUEUE_URL" ]]; then
  echo "ERROR: SQS_QUEUE_URL 비어있음 — terraform output sqs_reservation_url 확인" >&2
  exit 1
fi

tmux kill-session -t "$SESSION" 2>/dev/null || true

tmux new-session  -d -s "$SESSION" -x 220 -y 50 \
  -e SQS_QUEUE_URL="$SQS_QUEUE_URL" -e AWS_REGION="$AWS_REGION" \
  "cd '$APP_DIR' && bash scripts/observe/live-worker.sh 2"

tmux split-window -h -t "$SESSION" \
  -e SQS_QUEUE_URL="$SQS_QUEUE_URL" -e AWS_REGION="$AWS_REGION" \
  "kubectl -n ticketing logs -l app=worker-svc -f --tail=20 --max-log-requests=10"

tmux split-window -v -t "$SESSION":0.0 \
  "watch -n 2 'kubectl -n ticketing get pod -l app=worker-svc; echo ---; kubectl -n ticketing get hpa'"

tmux split-window -v -t "$SESSION":0.1 \
  -e SQS_QUEUE_URL="$SQS_QUEUE_URL" -e AWS_REGION="$AWS_REGION" \
  "cd '$APP_DIR' && bash scripts/observe/poll-replicas.sh 5 | tee /tmp/replicas.csv"

tmux attach -t "$SESSION"
