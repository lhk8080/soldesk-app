#!/usr/bin/env bash
# ticketing 클러스터 실시간 라이브 뷰 — worker-svc 중심
#
# 목적: 설계한 기능(KEDA → worker replica 증감, Cluster Autoscaler → 노드 증감)이
#       실제로 동작하는지 2~3초 단위로 터미널에 촘촘하게 출력해서 사람 눈으로 확인.
#       Prometheus/Grafana 경로의 pull 누적 지연(10초 이상)을 회피하기 위해
#       kubectl API 직접 폴링으로 구현.
#
# 사용:
#   bash scripts/live-worker.sh         # 2초 주기 (기본)
#   bash scripts/live-worker.sh 1       # 1초 주기
#   bash scripts/live-worker.sh 3       # 3초 주기
#
# 의존: kubectl (kubeconfig 설정 완료), aws CLI
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/terraform"

INTERVAL="${1:-2}"
NS="ticketing"

# 의존 체크
for cmd in kubectl aws; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: $cmd 이 필요합니다." >&2
    exit 1
  fi
done

# SQS URL / region — terraform output 으로만 얻는다 (계정 ID 하드코딩 금지).
# 수동 override 가 필요하면 SQS_QUEUE_URL env 변수로 주입:
#   export SQS_QUEUE_URL=$(terraform -chdir=terraform output -raw sqs_queue_url)
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

# ANSI color (Git Bash mintty / Linux / macOS 호환)
BOLD=$'\033[1m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
CYAN=$'\033[0;36m'
DIM=$'\033[2m'
RED=$'\033[0;31m'
NC=$'\033[0m'

# 이전 iteration의 노드/파드 이름 집합 (NEW 하이라이트용)
PREV_NODES=""
PREV_PODS=""

trap 'printf "\n종료.\n"; exit 0' INT

# ISO8601 → 상대 age ("30s" / "5m" / "2h" / "3d")
iso_to_age() {
  local iso="$1"
  if [[ -z "$iso" || "$iso" == "<none>" ]]; then
    echo "?"
    return
  fi
  local start_sec
  start_sec=$(date -d "$iso" +%s 2>/dev/null || echo 0)
  if [[ "$start_sec" == "0" ]]; then
    echo "?"
    return
  fi
  local now_sec=$(date +%s)
  local diff=$((now_sec - start_sec))
  if [[ $diff -lt 60 ]]; then
    echo "${diff}s"
  elif [[ $diff -lt 3600 ]]; then
    echo "$((diff/60))m"
  elif [[ $diff -lt 86400 ]]; then
    echo "$((diff/3600))h"
  else
    echo "$((diff/86400))d"
  fi
}

while true; do
  clear
  NOW=$(date '+%Y-%m-%d %H:%M:%S')
  printf "${BOLD}${CYAN}=== ticketing live  %s  (interval %ss)  Ctrl+C 종료 ==========${NC}\n" "$NOW" "$INTERVAL"

  # ── NODES ──────────────────────────────────────────────────
  NODE_LINES=$(kubectl get nodes --no-headers 2>/dev/null || true)
  NODE_COUNT=0
  CURR_NODES=""
  printf "\n${BOLD}NODES${NC}\n"
  if [[ -n "$NODE_LINES" ]]; then
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      N_NAME=$(echo "$line" | awk '{print $1}')
      N_STATUS=$(echo "$line" | awk '{print $2}')
      N_AGE=$(echo "$line" | awk '{print $4}')
      NODE_COUNT=$((NODE_COUNT + 1))
      CURR_NODES="${CURR_NODES}${N_NAME}"$'\n'
      if [[ -n "$PREV_NODES" ]] && ! printf '%s' "$PREV_NODES" | grep -qxF "$N_NAME"; then
        printf "  ${GREEN}%-55s  %-10s  %-6s  ← NEW${NC}\n" "$N_NAME" "$N_STATUS" "$N_AGE"
      elif [[ "$N_STATUS" != "Ready" ]]; then
        printf "  ${YELLOW}%-55s  %-10s  %s${NC}\n" "$N_NAME" "$N_STATUS" "$N_AGE"
      else
        printf "  %-55s  %-10s  %s\n" "$N_NAME" "$N_STATUS" "$N_AGE"
      fi
    done <<< "$NODE_LINES"
    printf "  ${DIM}total: %s${NC}\n" "$NODE_COUNT"
  else
    printf "  ${DIM}(nodes 조회 실패 — kubeconfig 확인)${NC}\n"
  fi

  # 1회 kubectl 호출로 deploy 전체 얻고 이후 재활용
  DEPLOY_LINES=$(kubectl get deploy -n "$NS" --no-headers 2>/dev/null || true)

  # ── WORKER-SVC DEPLOYMENT ──────────────────────────────────
  printf "\n${BOLD}WORKER-SVC${NC}\n"
  WORKER_LINE=$(printf '%s\n' "$DEPLOY_LINES" | awk '$1=="worker-svc"')
  if [[ -n "$WORKER_LINE" ]]; then
    W_READY=$(echo "$WORKER_LINE" | awk '{print $2}')
    W_UPTODATE=$(echo "$WORKER_LINE" | awk '{print $3}')
    W_AVAIL=$(echo "$WORKER_LINE" | awk '{print $4}')
    printf "  deployment : ${BOLD}ready=%s${NC}  up-to-date=%s  available=%s\n" \
      "$W_READY" "$W_UPTODATE" "$W_AVAIL"
  else
    printf "  ${DIM}(deployment 조회 실패)${NC}\n"
  fi

  # HPA (KEDA가 자동 생성한 keda-hpa-worker-svc 포함)
  HPA_ALL=$(kubectl get hpa -n "$NS" \
    -o custom-columns='NAME:.metadata.name,REF:.spec.scaleTargetRef.name,MIN:.spec.minReplicas,MAX:.spec.maxReplicas,CURRENT:.status.currentReplicas,DESIRED:.status.desiredReplicas' \
    --no-headers 2>/dev/null || true)
  WORKER_HPA=$(printf '%s\n' "$HPA_ALL" | awk '$2=="worker-svc"' | head -1)
  if [[ -n "$WORKER_HPA" ]]; then
    H_NAME=$(echo "$WORKER_HPA" | awk '{print $1}')
    H_MIN=$(echo "$WORKER_HPA" | awk '{print $3}')
    H_MAX=$(echo "$WORKER_HPA" | awk '{print $4}')
    H_CURR=$(echo "$WORKER_HPA" | awk '{print $5}')
    H_DES=$(echo "$WORKER_HPA" | awk '{print $6}')
    printf "  hpa        : ${BOLD}current=%s${NC}  desired=%s   min=%s  max=%s   ${DIM}(%s)${NC}\n" \
      "${H_CURR:-?}" "${H_DES:-?}" "${H_MIN:-?}" "${H_MAX:-?}" "$H_NAME"
  else
    printf "  hpa        : ${DIM}없음${NC}\n"
  fi

  # ScaledObject (KEDA) — 여러 개면 worker-svc targetting한 것만
  SO_LIST=$(kubectl get scaledobject -n "$NS" \
    -o jsonpath='{range .items[*]}{.metadata.name}|{.spec.scaleTargetRef.name}|{.status.conditions[?(@.type=="Ready")].status}|{.status.conditions[?(@.type=="Active")].status}{"\n"}{end}' \
    2>/dev/null || true)
  SO_LINE=$(printf '%s\n' "$SO_LIST" | awk -F'|' '$2=="worker-svc"' | head -1)
  if [[ -n "$SO_LINE" ]]; then
    SO_NAME=$(echo "$SO_LINE" | awk -F'|' '{print $1}')
    SO_READY=$(echo "$SO_LINE" | awk -F'|' '{print $3}')
    SO_ACTIVE=$(echo "$SO_LINE" | awk -F'|' '{print $4}')
    AC_COLOR="$NC"
    [[ "$SO_ACTIVE" == "True" ]] && AC_COLOR="$GREEN"
    printf "  scaledobj  : ready=%s  ${AC_COLOR}active=%s${NC}   ${DIM}(%s)${NC}\n" \
      "${SO_READY:-?}" "${SO_ACTIVE:-?}" "${SO_NAME:-?}"
  else
    printf "  scaledobj  : ${DIM}없음${NC}\n"
  fi

  # worker-svc 파드 목록 (custom-columns 로 RESTARTS 공백 문제 회피)
  POD_LINES=$(kubectl get pods -n "$NS" \
    -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName,START:.status.startTime' \
    --no-headers 2>/dev/null | awk '$1 ~ /^worker-svc-/' || true)
  POD_COUNT=0
  CURR_PODS=""
  printf "\n  pods:\n"
  if [[ -n "$POD_LINES" ]]; then
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      P_NAME=$(echo "$line" | awk '{print $1}')
      P_PHASE=$(echo "$line" | awk '{print $2}')
      P_NODE=$(echo "$line" | awk '{print $3}')
      P_START=$(echo "$line" | awk '{print $4}')
      P_AGE=$(iso_to_age "$P_START")
      POD_COUNT=$((POD_COUNT + 1))
      CURR_PODS="${CURR_PODS}${P_NAME}"$'\n'
      if [[ -n "$PREV_PODS" ]] && ! printf '%s' "$PREV_PODS" | grep -qxF "$P_NAME"; then
        printf "    ${GREEN}%-42s  %-11s  %-6s  %s  ← NEW${NC}\n" "$P_NAME" "$P_PHASE" "$P_AGE" "$P_NODE"
      elif [[ "$P_PHASE" != "Running" ]]; then
        printf "    ${YELLOW}%-42s  %-11s  %-6s  %s${NC}\n" "$P_NAME" "$P_PHASE" "$P_AGE" "$P_NODE"
      else
        printf "    %-42s  %-11s  %-6s  %s\n" "$P_NAME" "$P_PHASE" "$P_AGE" "$P_NODE"
      fi
    done <<< "$POD_LINES"
    printf "    ${DIM}total: %s${NC}\n" "$POD_COUNT"
  else
    printf "    ${DIM}(worker 파드 없음)${NC}\n"
  fi

  # ── SQS ────────────────────────────────────────────────────
  printf "\n${BOLD}SQS${NC}  ${DIM}(reservation.fifo)${NC}\n"
  SQS_ATTR=$(aws sqs get-queue-attributes \
    --queue-url "$SQS_URL" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
    --region "$REGION" \
    --query 'Attributes.[ApproximateNumberOfMessages, ApproximateNumberOfMessagesNotVisible]' \
    --output text 2>/dev/null || echo "? ?")
  VISIBLE=$(echo "$SQS_ATTR" | awk '{print $1}')
  INFLIGHT=$(echo "$SQS_ATTR" | awk '{print $2}')
  printf "  reservation.fifo:\n"
  printf "    visible   : ${BOLD}%s${NC}\n" "${VISIBLE:-?}"
  printf "    in-flight : %s\n" "${INFLIGHT:-?}"

  # ── READ / WRITE API alive check ────────────────────────────
  printf "\n${BOLD}READ / WRITE API${NC} ${DIM}(alive check)${NC}\n"
  RA_LINE=$(printf '%s\n' "$DEPLOY_LINES" | awk '$1=="read-api"')
  WA_LINE=$(printf '%s\n' "$DEPLOY_LINES" | awk '$1=="write-api"')
  RA_READY=$(echo "$RA_LINE" | awk '{print $2}')
  WA_READY=$(echo "$WA_LINE" | awk '{print $2}')
  printf "  read-api      : %s\n" "${RA_READY:-?}"
  printf "  write-api     : %s\n" "${WA_READY:-?}"

  # diff 비교용 이전 상태 업데이트 (NEW 표기는 한 iteration만)
  PREV_NODES="$CURR_NODES"
  PREV_PODS="$CURR_PODS"

  sleep "$INTERVAL"
done
