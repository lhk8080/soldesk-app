## 3-repo 구조

| 레포지토리명 | 책임 범위 | 링크 |
|---|---|---|
| soldesk-infra | • 테라폼 코드 전반<br>• AWS 리소스 + IAM<br>• 클러스터 운용에 필요한 애드온 (helm provider) | [github](https://github.com/lhk8080/soldesk-infra) |
| soldesk-k8s | • ArgoCD에 의해 동기화되는 대상<br>• monitoring, service app | [github](https://github.com/lhk8080/soldesk-k8s) |
| **soldesk-app** (이 repo) | • 애플리케이션 소스 코드<br>• 이미지 빌드 & 레지스트리 푸시 지점 | — |

## application 구성

| 서비스 | 종류 | 역할 |
|---|---|---|
| `ticketing-was` (write-api) | 예매 / 결제 등 상태 변경 요청, SQS 메시지 생성|
| `ticketing-was` (read-api) | 조회 전용 (공연·좌석 등), 캐시 우선 |
| `worker-svc` | SQS 메시지 소비 → DB 커밋 + Redis 반영 (for 비동기 처리) |
| `frontend` | S3 + CloudFront를 통해 배포|

> WAS 는 동일 코드베이스(`backend/ticketing-was`)를 가지고, `read_app.py` / `write_app.py` 두 진입점이 존재함. k8s 차트에서 별도 Deployment를 가지고 배포됨.

## 디렉토리 구조

```
soldesk-app/
├── backend/
│   ├── ticketing-was/    # FastAPI — write/read 진입점 분리
│   └── worker-svc/       # SQS consumer (예약 메시지 처리)
├── frontend/             # 정적 HTML/CSS/JS (Single Page Application 형식으로 S3에 정적 호스팅)
├── db-schema/
│   ├── create.sql        # 초기 스키마
│   ├── seed.sql          # 시연용 더미 데이터
│   └── migrations/       # 누적 마이그레이션
├── scripts/              # 부하·관측·운영 스크립트
└── seed.sh               # 초기 배포 진입점

```

## 빌드 / 이미지 배포 흐름

```
backend/ticketing-was ──┐
                        ├─ docker build ─→ ECR push ─→ values.yaml tag bump ─→ ArgoCD sync ─→ EKS pod
backend/worker-svc    ──┘

frontend/src/         ───────────────────→ S3 sync ─→ CloudFront invalidation
```

- **이미지 태그**:  커밋 해시값을 그대로 사용
- **레지스트리**: `infra` 모듈의 ECR
- **k8s 반영**: `soldesk-k8s/environments/<env>/ticketing-values.yaml` 의 `image.was.tag` / `image.worker.tag` 를 새 SHA 로 변경 → commit/push → ArgoCD 자동 sync.
- **frontend**: 빌드 단계 없음. `frontend/src/` 정적 파일을 그대로 S3 sync + CloudFront invalidation.

## seed.sh
`apply.sh` 이후 한 번 돌리면 끝나는 초기 배포 스크립트. 단계:

1. **infra outputs 읽기** — ECR URL, RDS endpoint, SQS URL, ESO Role 등 런타임 값 수집
2. **DB 마이그레이션** — ephemeral `mysql-init` Job 으로 `create.sql` / `migrations/*.sql` 적용. 
3. **dev DB 분리 생성** (prod 환경에서만) — 같은 RDS 인스턴스 안에 `ticketing_dev` 스키마 + 전용 user 생성
4. **ECR build + push** — was / worker 이미지 빌드 후 SHA 태그로 push
5. **k8s image tag 반영** — Application 의 image tag 를 새 SHA 로 갱신 → ArgoCD sync 트리거
6. **frontend 배포** — S3 sync + CloudFront invalidation, `index.html` 에 Cognito · API origin 동적 주입
