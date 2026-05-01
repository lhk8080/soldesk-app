# soldesk-app

## scripts/ 구조

| 디렉토리 | 용도 |
|---|---|
| `scripts/load/` | SQS·HTTP 부하 생성 (boto3, locust) |
| `scripts/observe/` | 라이브 뷰, CSV 폴링, 큐 소진 대기 |
| `scripts/control/` | KEDA·worker-svc 오토스케일 ON/OFF |
| `scripts/db/` | DB 디버깅 조회 |

## 부하테스트 스크립트 환경 세팅

`scripts/` 안의 SQS 부하 스크립트(boto3 사용)와 locust를 돌리려면 venv에 패키지를 깔아야 합니다.

### 최초 1회

```bash
sudo apt install python3.12-venv     # 우분투에 venv 모듈이 없으면
cd /home/lhk64/soldesk-app
python3 -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements.txt
```

### 이후 새 셸 열 때마다

```bash
source /home/lhk64/soldesk-app/.venv/bin/activate
```

프롬프트 앞에 `(.venv)`가 붙으면 활성화 OK. 빠져나올 땐 `deactivate`.

### 패키지 추가했을 때

```bash
pip install <새 패키지>
pip freeze > scripts/requirements.txt   # 목록 갱신
```
