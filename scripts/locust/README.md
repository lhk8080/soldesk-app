## Locust로 콘서트3 최소 검증

이 디렉토리의 `concert3_locustfile.py`는 **연출/보정 로직을 제거**하고,
서버가 설계대로 흘러가는지 아래 3가지만 보도록 만든 최소 시나리오입니다.

- **대기열 생성/통과**: `waiting-room/enter` 후 `waiting-room/status` 폴링에서 `ADMITTED` 전환
- **백그라운드 처리량 생성**: `ADMITTED` 된 일부 유저만 `booking/commit`(=write)
- **최종 정합성/실패율**: `booking/status/{booking_ref}` 폴링으로 최종 `OK/FAIL` 집계

### 실행 (로컬/VM)

1) 설치

```bash
pip install locust
```

2) 환경변수 지정 후 실행

```bash
export WRITE_API_BASE_URL=http://write-api.ticketing.svc.cluster.local:5001
export CONCERT_SHOW_ID=8
# locust만 직접 띄우면 파일 기본 COMMIT_FRACTION=0.05 → 유저 수 적으면 커밋이 거의 0건
# 스모크는 export COMMIT_FRACTION=1 권장. run_concert3.sh 는 미설정 시 1.0으로 둠.
export COMMIT_FRACTION=1
locust -f scripts/locust/concert3_locustfile.py
```

브라우저 UI에서
- Users: N (동시 유저 수)
- Spawn rate: 초당 증가 유저 수
- Host: `WRITE_API_BASE_URL`과 동일한 값(예: `http://...:5001`)

### 튜닝 포인트(자주 만지는 env)

- `COMMIT_FRACTION`: admitted 유저 중 write까지 가는 비율. **`concert3_locustfile.py`만 쓰면 기본 0.05**, **`run_concert3.sh`는 미설정 시 1.0** (소수 유저 스모크에서도 `Commit (QUEUED)` / `Booking status`가 나오게)
- `ADMIT_TIMEOUT_SEC`: admitted 기다리는 최대 시간(기본 600)
- `BOOKING_TIMEOUT_SEC`: 최종 OK/FAIL 기다리는 최대 시간(기본 600)
- `SEAT_ROWS`, `SEAT_COLS`: 서버 좌석 규칙과 맞추기(기본 200x250)
- `ADD_TRACE_ID`: `X-Trace-Id` 헤더 추가 여부(기본 true)

### 관측(권장)

- Locust 통계: `WR enter`, `WR status`, `Commit (QUEUED)`, `Booking status`의 RPS/실패율/지연
- 서버/CloudWatch: SQS backlog, oldest age, received/deleted 등으로 "쌓였다가 빠지는지" 확인

