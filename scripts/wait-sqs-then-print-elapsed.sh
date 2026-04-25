#!/usr/bin/env bash
# run_concert_then_snapshot 계열 후처리:
# - terraform output sqs_queue_url 로 큐 URL을 구하고
# - 큐(가시+인플라이트+지연)가 비워질 때까지 대기한 뒤
# - 소진까지 걸린 시간을 출력한다.
#
# 사용:
#   bash scripts/wait-sqs-then-print-elapsed.sh [TIMEOUT_SEC] [POLL_SEC]
#
# 필요: aws, terraform, python3

_script="${BASH_SOURCE[0]}"
if command -v grep >/dev/null 2>&1 && grep -q $'\r' "$_script" 2>/dev/null; then
  if sed --version >/dev/null 2>&1; then
    sed -i 's/\r$//' "$_script"
  else
    sed -i '' 's/\r$//' "$_script"
  fi
  exec bash "$_script" "$@"
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
TIMEOUT_SEC="${1:-900}"
POLL_SEC="${2:-2}"

QUEUE_URL="$(bash "$SCRIPT_DIR/tf-out.sh" sqs_queue_url 2>/dev/null || true)"
QUEUE_URL="$(printf "%s" "$QUEUE_URL" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
if [[ -z "${QUEUE_URL:-}" ]]; then
  echo "ERROR: terraform output sqs_queue_url is empty" >&2
  exit 1
fi

t0="$(date +%s)"
json="$(bash "$SCRIPT_DIR/wait_sqs_queue_empty.sh" "$QUEUE_URL" "$TIMEOUT_SEC" "$POLL_SEC")"
t1="$(date +%s)"

elapsed="$((t1 - t0))"
echo "sqs drain wait: ${elapsed}s"
echo "$json"

