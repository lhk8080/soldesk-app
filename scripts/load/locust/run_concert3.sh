#!/usr/bin/env bash
set -euo pipefail

# Locust --host를 자동 설정해 실행한다.
# 우선순위:
# 1) WRITE_API_BASE_URL (kubectl로 뽑아 이미 export 해둔 값)
# 2) LOCUST_HOST (수동 오버라이드)
# 3) terraform output -raw zzzzzz_url (frontend_website_url과 동일)
# 4) (kubectl 사용 가능할 때만) write-api svc로 클러스터 내부 URL 생성

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TERRAFORM_DIR="${TERRAFORM_DIR:-$REPO_ROOT/terraform}"
TF_OUTPUT_KEY="${TF_OUTPUT_KEY:-zzzzzz_url}"
KUBECTL_NAMESPACE="${KUBECTL_NAMESPACE:-ticketing}"
WRITE_API_SERVICE_NAME="${WRITE_API_SERVICE_NAME:-write-api}"

_require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

_trim() {
  # shellcheck disable=SC2001
  echo "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

_detect_host() {
  local host="${WRITE_API_BASE_URL:-}"
  host="$(_trim "$host")"
  if [ -n "$host" ]; then
    echo "$host"
    return 0
  fi

  host="${LOCUST_HOST:-}"
  host="$(_trim "$host")"
  if [ -n "$host" ]; then
    echo "$host"
    return 0
  fi

  if command -v terraform >/dev/null 2>&1; then
    if [ ! -d "$TERRAFORM_DIR" ]; then
      echo "Terraform dir not found: $TERRAFORM_DIR" >&2
      exit 1
    fi

    # -raw는 null이면 에러를 내거나 빈값이 될 수 있어 안전하게 처리
    host="$(terraform -chdir="$TERRAFORM_DIR" output -raw "$TF_OUTPUT_KEY" 2>/dev/null || true)"
    host="$(_trim "$host")"
    if [ -n "$host" ] && [ "$host" != "null" ]; then
      echo "$host"
      return 0
    fi
  fi

  # 마지막 fallback: kubectl이 있을 때만, write-api svc로 내부 주소 생성
  if command -v kubectl >/dev/null 2>&1; then
    local port
    port="$(kubectl get svc "$WRITE_API_SERVICE_NAME" -n "$KUBECTL_NAMESPACE" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || true)"
    port="$(_trim "$port")"
    if [ -n "$port" ]; then
      echo "http://${WRITE_API_SERVICE_NAME}.${KUBECTL_NAMESPACE}.svc.cluster.local:${port}"
      return 0
    fi
  fi

  echo "Failed to determine Locust host." >&2
  echo "Set one of: WRITE_API_BASE_URL, LOCUST_HOST, or make terraform output '$TF_OUTPUT_KEY' available." >&2
  echo "If you want kubectl auto-detect, ensure kubectl is installed and can access the cluster." >&2
  exit 1
}

HOST="$(_detect_host)"

if [[ "$HOST" != http://* && "$HOST" != https://* ]]; then
  echo "Locust host must include scheme (http/https). Got: $HOST" >&2
  exit 1
fi

# admitted → commit까지 갈 비율. locustfile 기본은 0.05라 소수 유저(-u 5 등) 스모크에서는
# 거의 항상 커밋 요청이 0건으로 끝난다. 이 래퍼는 끝단까지 검증하려는 경우가 많아 기본 1.0.
# 부분 유입만 시뮬레이션할 때: COMMIT_FRACTION=0.05 ./run_concert3.sh ...
export COMMIT_FRACTION="${COMMIT_FRACTION:-1}"

# 아래 인자는 필요에 따라 호출자가 그대로 전달한다.
# 예:
#   CONCERT_SHOW_ID=8 ./run_concert3.sh --headless -u 200 -r 20 -t 5m
exec locust -f "$SCRIPT_DIR/concert3_locustfile.py" -H "$HOST" "$@"

