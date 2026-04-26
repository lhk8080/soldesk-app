#!/bin/bash
# seed.sh — 초기 배포용 스크립트
# 실행 순서: DB 마이그레이션 → ECR build+push → ArgoCD image tag 갱신 → S3 sync + CF invalidation
#
# 사전 조건:
#   - apply.sh 완료 (infra + k8s 배포 완료)
#   - kubectl, aws CLI, docker 설치 및 인증 완료
#   - AWS_REGION, TF_STATE_BUCKET 환경 변수 설정
#
# 선택 옵션:
#   SKIP_BUILD=1       — ECR 빌드/푸시 생략 (이미 이미지가 있을 때)
#   SKIP_MIGRATE=1     — DB 마이그레이션 생략
#   SKIP_FRONTEND=1    — S3 sync / CF invalidation 생략
#   INFRA_DIR=<path>   — soldesk-infra 경로 (기본: ../soldesk-infra)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 환경 변수 ──────────────────────────────────────────────────────────────
: "${AWS_REGION:=ap-northeast-2}"
: "${SSM_PREFIX:=/ticketing/prod}"

SKIP_BUILD="${SKIP_BUILD:-0}"
SKIP_MIGRATE="${SKIP_MIGRATE:-0}"
SKIP_FRONTEND="${SKIP_FRONTEND:-0}"

INFRA_DIR="${INFRA_DIR:-$(cd "${SCRIPT_DIR}/../soldesk-infra" 2>/dev/null && pwd || echo "")}"
if [[ -z "${INFRA_DIR}" ]]; then
  echo "ERROR: soldesk-infra 디렉토리를 찾을 수 없습니다. INFRA_DIR 환경 변수를 지정하세요."
  exit 1
fi

if [[ -z "${TF_STATE_BUCKET:-}" ]]; then
  echo ">>> TF_STATE_BUCKET 미설정 — bootstrap output에서 읽기"
  cd "${INFRA_DIR}/bootstrap"
  TF_STATE_BUCKET=$(terraform output -raw s3_bucket_name)
  echo "    TF_STATE_BUCKET=${TF_STATE_BUCKET}"
fi

# ── 1. infra terraform outputs 읽기 ───────────────────────────────────────
echo ">>> [1/5] infra outputs 읽기"
cd "${INFRA_DIR}/infra"

tf_output() { terraform output -raw "$1"; }

CLUSTER_NAME=$(tf_output cluster_name)
ECR_WAS_URL=$(tf_output ecr_ticketing_was_url)
ECR_WORKER_URL=$(tf_output ecr_worker_svc_url)
FRONTEND_BUCKET=$(tf_output frontend_bucket_id)
CF_DOMAIN=$(tf_output cloudfront_domain)
SQS_ACCESS_ROLE_ARN=$(tf_output sqs_access_role_arn)
DB_BACKUP_ROLE_ARN=$(tf_output db_backup_role_arn)
ESO_ROLE_ARN=$(tf_output eso_role_arn)
SQS_QUEUE_URL=$(tf_output sqs_reservation_url)
ASSETS_BUCKET=$(tf_output assets_bucket_id)

ECR_REGISTRY="${ECR_WAS_URL%%/*}"  # account.dkr.ecr.region.amazonaws.com

echo "  cluster     : ${CLUSTER_NAME}"
echo "  ECR WAS     : ${ECR_WAS_URL}"
echo "  ECR worker  : ${ECR_WORKER_URL}"
echo "  Frontend S3 : ${FRONTEND_BUCKET}"
echo "  CloudFront  : ${CF_DOMAIN}"

# ── 2. kubeconfig 업데이트 ─────────────────────────────────────────────────
echo ""
echo ">>> [2/5] kubeconfig 업데이트"
aws eks update-kubeconfig \
  --name "${CLUSTER_NAME}" \
  --region "${AWS_REGION}"

# ── 3. DB 마이그레이션 (Kubernetes Job) ────────────────────────────────────
if [[ "${SKIP_MIGRATE}" == "1" ]]; then
  echo ""
  echo ">>> [3/5] DB 마이그레이션 건너뜀 (SKIP_MIGRATE=1)"
else
  echo ""
  echo ">>> [3/5] DB 마이그레이션"

  # SSM에서 DB 접속 정보 읽기
  ssm_get() {
    aws ssm get-parameter \
      --name "${SSM_PREFIX}/$1" \
      --with-decryption \
      --query Parameter.Value \
      --output text \
      --region "${AWS_REGION}"
  }

  DB_HOST=$(ssm_get DB_WRITER_HOST)
  DB_USER=$(ssm_get DB_USER)
  DB_PASSWORD=$(ssm_get DB_PASSWORD)

  # migration 파일 목록 (이름순 정렬)
  MIGRATION_DIR="${SCRIPT_DIR}/db-schema/migrations"
  MIGRATION_FILES=()
  while IFS= read -r -d '' f; do
    MIGRATION_FILES+=("$f")
  done < <(find "${MIGRATION_DIR}" -name "*.sql" -print0 | sort -z)

  # SQL ConfigMap 생성 (멱등: --dry-run + apply)
  echo "  ConfigMap(db-migration-sql) 생성..."
  kubectl create configmap db-migration-sql \
    --from-file=create.sql="${SCRIPT_DIR}/db-schema/create.sql" \
    --from-file=seed.sql="${SCRIPT_DIR}/db-schema/seed.sql" \
    $(for f in "${MIGRATION_FILES[@]}"; do echo "--from-file=$(basename "$f")=${f}"; done) \
    --namespace=default \
    --dry-run=client -o yaml | kubectl apply -f -

  # runner.sh ConfigMap 생성
  echo "  ConfigMap(db-migration-runner) 생성..."
  MIGRATION_NAMES=()
  for f in "${MIGRATION_FILES[@]}"; do
    MIGRATION_NAMES+=("$(basename "$f" .sql)")
  done

  kubectl create configmap db-migration-runner \
    --from-literal=runner.sh="$(cat <<'RUNNER_EOF'
#!/bin/bash
set -e

MYSQL="mysql -h ${DB_HOST} -u ${DB_USER} -p${DB_PASSWORD} --connect-timeout=10 --default-character-set=utf8mb4"

echo "[migrate] create.sql 적용 (idempotent)..."
$MYSQL < /sql/create.sql

echo "[migrate] seed.sql 적용 (idempotent)..."
$MYSQL ticketing < /sql/seed.sql

echo "[migrate] schema_migrations 테이블 확인..."
$MYSQL ticketing -e "
  CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(255) PRIMARY KEY,
    applied_at DATETIME DEFAULT NOW()
  );
"

ROWS=$($MYSQL ticketing -sN -e "SELECT COUNT(*) FROM schema_migrations")
if [ "${ROWS}" = "0" ]; then
  echo "[migrate] schema_migrations 비어있음 — create.sql 이 최신 스키마를 포함하므로 모든 migration 을 자동 마킹."
  for version in ${MIGRATION_VERSIONS}; do
    echo "[migrate] ${version} 자동 마킹"
    $MYSQL ticketing -e "INSERT INTO schema_migrations (version) VALUES ('${version}')"
  done
else
  for version in ${MIGRATION_VERSIONS}; do
    already=$($MYSQL ticketing -sN -e "SELECT COUNT(*) FROM schema_migrations WHERE version='${version}'")
    if [ "${already}" = "0" ]; then
      echo "[migrate] ${version} 적용..."
      $MYSQL ticketing < "/sql/${version}.sql"
      $MYSQL ticketing -e "INSERT INTO schema_migrations (version) VALUES ('${version}')"
      echo "[migrate] ${version} 완료"
    else
      echo "[migrate] ${version} 건너뜀 (이미 적용됨)"
    fi
  done
fi

echo "[migrate] 완료"
RUNNER_EOF
)" \
    --namespace=default \
    --dry-run=client -o yaml | kubectl apply -f -

  # DB 자격증명 Secret 생성
  echo "  Secret(db-migration-creds) 생성..."
  kubectl create secret generic db-migration-creds \
    --from-literal=DB_HOST="${DB_HOST}" \
    --from-literal=DB_USER="${DB_USER}" \
    --from-literal=DB_PASSWORD="${DB_PASSWORD}" \
    --namespace=default \
    --dry-run=client -o yaml | kubectl apply -f -

  # 기존 Job 삭제 (재실행 지원)
  kubectl delete job db-migration --namespace=default --ignore-not-found

  # Job 생성
  echo "  Job(db-migration) 생성..."
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: db-migration
  namespace: default
spec:
  backoffLimit: 2
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: mysql:8.0
          command: ["/bin/bash", "/runner/runner.sh"]
          env:
            - name: DB_HOST
              valueFrom:
                secretKeyRef:
                  name: db-migration-creds
                  key: DB_HOST
            - name: DB_USER
              valueFrom:
                secretKeyRef:
                  name: db-migration-creds
                  key: DB_USER
            - name: DB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: db-migration-creds
                  key: DB_PASSWORD
            - name: MIGRATION_VERSIONS
              value: "$(IFS=' '; echo "${MIGRATION_NAMES[*]}")"
          volumeMounts:
            - name: sql
              mountPath: /sql
            - name: runner
              mountPath: /runner
      volumes:
        - name: sql
          configMap:
            name: db-migration-sql
        - name: runner
          configMap:
            name: db-migration-runner
            defaultMode: 0755
EOF

  echo "  Job 완료 대기 (timeout 30m)..."
  kubectl wait job/db-migration \
    --namespace=default \
    --for=condition=complete \
    --timeout=1800s

  echo "  마이그레이션 로그:"
  kubectl logs job/db-migration --namespace=default

  # 리소스 정리
  echo "  임시 리소스 정리..."
  kubectl delete configmap db-migration-sql db-migration-runner --namespace=default --ignore-not-found
  kubectl delete secret db-migration-creds --namespace=default --ignore-not-found
fi

# ── 4. ECR build + push ────────────────────────────────────────────────────
WAS_TAG=""
WORKER_TAG=""

if [[ "${SKIP_BUILD}" == "1" ]]; then
  echo ""
  echo ">>> [4/5] ECR build/push 건너뜀 (SKIP_BUILD=1)"
  # 기존 태그 유지
  WAS_TAG=$(kubectl get application ticketing -n argocd \
    -o jsonpath='{.spec.source.helm.parameters[?(@.name=="image.was.tag")].value}' \
    2>/dev/null || echo "")
  WORKER_TAG=$(kubectl get application ticketing -n argocd \
    -o jsonpath='{.spec.source.helm.parameters[?(@.name=="image.worker.tag")].value}' \
    2>/dev/null || echo "")
else
  echo ""
  echo ">>> [4/5] ECR build + push"

  # ECR 로그인
  aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${ECR_REGISTRY}"

  # ticketing-was 빌드
  WAS_SHA=$(cd "${SCRIPT_DIR}/backend/ticketing-was" && git rev-parse --short HEAD)
  WAS_TAG="${WAS_SHA}"
  echo "  ticketing-was 빌드 (tag: ${WAS_TAG})..."
  docker build \
    -t "${ECR_WAS_URL}:${WAS_TAG}" \
    -t "${ECR_WAS_URL}:latest" \
    "${SCRIPT_DIR}/backend/ticketing-was"
  docker push "${ECR_WAS_URL}:${WAS_TAG}"
  docker push "${ECR_WAS_URL}:latest"

  # worker-svc 빌드
  WORKER_SHA=$(cd "${SCRIPT_DIR}/backend/worker-svc" && git rev-parse --short HEAD)
  WORKER_TAG="${WORKER_SHA}"
  echo "  worker-svc 빌드 (tag: ${WORKER_TAG})..."
  docker build \
    -t "${ECR_WORKER_URL}:${WORKER_TAG}" \
    -t "${ECR_WORKER_URL}:latest" \
    "${SCRIPT_DIR}/backend/worker-svc"
  docker push "${ECR_WORKER_URL}:${WORKER_TAG}"
  docker push "${ECR_WORKER_URL}:latest"
fi

# ── 5. ArgoCD Application image tag 갱신 ──────────────────────────────────
echo ""
echo ">>> [5a/5] ArgoCD Application(ticketing) image tag 갱신"

# 태그가 결정되지 않은 경우 (SKIP_BUILD=1이고 기존 태그도 없는 경우) 경고
if [[ -z "${WAS_TAG}" || "${WAS_TAG}" == "seed-pending" ]]; then
  echo "WARNING: WAS_TAG 미결정. image.was.tag 를 직접 지정하세요."
  WAS_TAG="seed-pending"
fi
if [[ -z "${WORKER_TAG}" || "${WORKER_TAG}" == "seed-pending" ]]; then
  echo "WARNING: WORKER_TAG 미결정. image.worker.tag 를 직접 지정하세요."
  WORKER_TAG="seed-pending"
fi

echo "  was.tag    : ${WAS_TAG}"
echo "  worker.tag : ${WORKER_TAG}"

kubectl apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ticketing
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "10"
spec:
  project: default
  source:
    repoURL: https://github.com/lhk8080/soldesk-k8s.git
    targetRevision: HEAD
    path: charts/ticketing
    helm:
      valueFiles:
        - ../../environments/prod/ticketing-values.yaml
      parameters:
        - name: image.was.repository
          value: "${ECR_WAS_URL}"
        - name: image.was.tag
          value: "${WAS_TAG}"
        - name: image.worker.repository
          value: "${ECR_WORKER_URL}"
        - name: image.worker.tag
          value: "${WORKER_TAG}"
        - name: serviceAccount.sqsAccessRoleArn
          value: "${SQS_ACCESS_ROLE_ARN}"
        - name: serviceAccount.dbBackupRoleArn
          value: "${DB_BACKUP_ROLE_ARN}"
        - name: config.sqsQueueUrl
          value: "${SQS_QUEUE_URL}"
        - name: backup.s3Bucket
          value: "${ASSETS_BUCKET}"
  destination:
    server: https://kubernetes.default.svc
    namespace: ticketing
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
EOF

# ── 6. Frontend S3 sync + CloudFront invalidation ─────────────────────────
if [[ "${SKIP_FRONTEND}" == "1" ]]; then
  echo ""
  echo ">>> [5b/5] Frontend 배포 건너뜀 (SKIP_FRONTEND=1)"
else
  echo ""
  echo ">>> [5b/5] Frontend S3 sync + CloudFront invalidation"

  FRONTEND_SRC="${SCRIPT_DIR}/frontend/src"
  if [[ ! -d "${FRONTEND_SRC}" ]]; then
    echo "ERROR: ${FRONTEND_SRC} 디렉토리가 없습니다."
    exit 1
  fi

  # index.html 에 Cognito + API origin 인라인 주입
  COGNITO_CLIENT_ID=$(cd "${INFRA_DIR}/infra" && terraform output -raw cognito_client_id)
  COGNITO_USER_POOL_ID=$(cd "${INFRA_DIR}/infra" && terraform output -raw cognito_user_pool_id)
  # CloudFront 경유 same-origin 호출이므로 API_ORIGIN 은 빈 문자열
  API_ORIGIN=""

  TMP_INDEX=$(mktemp)
  sed "s|<script src=\"/api-origin.js\"></script>|<script>window.__TICKETING_API_ORIGIN__=\"${API_ORIGIN}\";window.COGNITO_CONFIG={REGION:\"${AWS_REGION}\",CLIENT_ID:\"${COGNITO_CLIENT_ID}\",USER_POOL_ID:\"${COGNITO_USER_POOL_ID}\"};</script>|" \
    "${FRONTEND_SRC}/index.html" > "${TMP_INDEX}"

  echo "  S3 sync → s3://${FRONTEND_BUCKET}/"
  aws s3 sync "${FRONTEND_SRC}" "s3://${FRONTEND_BUCKET}/" \
    --delete \
    --exclude "index.html" \
    --region "${AWS_REGION}"

  aws s3 cp "${TMP_INDEX}" "s3://${FRONTEND_BUCKET}/index.html" \
    --content-type "text/html; charset=utf-8" \
    --cache-control "no-store, max-age=0" \
    --region "${AWS_REGION}"
  rm -f "${TMP_INDEX}"

  # CloudFront distribution ID 조회
  CF_DIST_ID=$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?DomainName=='${CF_DOMAIN}'].Id" \
    --output text \
    --region "${AWS_REGION}" | head -1)

  if [[ -z "${CF_DIST_ID}" ]]; then
    echo "WARNING: CloudFront distribution ID 를 찾을 수 없습니다. Invalidation 건너뜀."
  else
    echo "  CloudFront invalidation (${CF_DIST_ID})..."
    aws cloudfront create-invalidation \
      --distribution-id "${CF_DIST_ID}" \
      --paths "/*" \
      --region "${AWS_REGION}"
  fi
fi

# ── 완료 ──────────────────────────────────────────────────────────────────
echo ""
echo "=== seed.sh 완료 ==="
echo ""
if [[ "${SKIP_BUILD}" != "1" ]]; then
  echo "  was.tag    : ${WAS_TAG}"
  echo "  worker.tag : ${WORKER_TAG}"
fi
echo ""
echo "[다음 단계] ArgoCD sync 상태 확인:"
echo "  kubectl get application ticketing -n argocd"
echo "  kubectl get pods -n ticketing"
