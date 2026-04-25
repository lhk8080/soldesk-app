#!/usr/bin/env bash
# SQS 큐의 가시 + 인플라이트 + 지연 메시지 합이 0인 상태가 연속 2회(폴링 간격마다)면 종료.
# 사용: wait_sqs_queue_empty.sh QUEUE_URL [TIMEOUT_SEC] [POLL_SEC]
#   TIMEOUT_SEC 기본 900(정수 초), POLL_SEC 기본 2
# 성공 시 stdout: JSON 한 줄
# 타임아웃 시 동일 형식이 stderr, exit 1
# 필요: aws CLI, python3
set -euo pipefail

QUEUE_URL="${1:?첫 인자: SQS 큐 URL}"
TIMEOUT_INT="${2:-900}"
POLL_SEC="${3:-2}"

t0=$(date +%s)
stable=0
last_vis=0 last_inv=0 last_del=0

depth_json() {
  aws sqs get-queue-attributes \
    --queue-url "$QUEUE_URL" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible ApproximateNumberOfMessagesDelayed \
    --output json
}

parse_depths() {
  python3 -c "
import json, sys
a = json.load(sys.stdin).get('Attributes') or {}
def i(k):
    try:
        return int(str(a.get(k) or '0').strip() or '0', 10)
    except ValueError:
        return 0
print(i('ApproximateNumberOfMessages'), i('ApproximateNumberOfMessagesNotVisible'), i('ApproximateNumberOfMessagesDelayed'))
"
}

emit_json() {
  local elapsed="$1" timeout_flag="$2"
  printf '{"큐_소진_대기_초":%s,"큐_대기_타임아웃":%s,"큐_종료_가시":%s,"큐_종료_인플라이트":%s,"큐_종료_지연":%s}\n' \
    "$elapsed" "$timeout_flag" "$last_vis" "$last_inv" "$last_del"
}

while true; do
  now=$(date +%s)
  if (( now - t0 >= TIMEOUT_INT )); then
    read -r vis inv del <<< "$(depth_json | parse_depths)"
    last_vis=$vis last_inv=$inv last_del=$del
    elapsed=$((now - t0))
    emit_json "$elapsed" true >&2
    exit 1
  fi
  read -r vis inv del <<< "$(depth_json | parse_depths)"
  last_vis=$vis last_inv=$inv last_del=$del
  tot=$((vis + inv + del))
  if (( tot == 0 )); then
    stable=$((stable + 1))
    if (( stable >= 2 )); then
      now=$(date +%s)
      elapsed=$((now - t0))
      emit_json "$elapsed" false
      exit 0
    fi
  else
    stable=0
  fi
  sleep "$POLL_SEC"
done
