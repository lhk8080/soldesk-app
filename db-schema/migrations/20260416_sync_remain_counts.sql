-- remain_count 동기화 (수동 실행용 SQL; Terraform 자동 적용 없음)
-- 목적:
-- - schedules.remain_count / concert_shows.remain_count 를 ACTIVE 예약좌석 기준으로 재계산
-- - status도 remain 기준으로 OPEN/CLOSED로 정렬
-- - 예약이 0이고 값이 이미 정상인 행은 UPDATE를 스킵해 쓰기 부하를 줄임

USE ticketing;

-- 영화(극장) 회차
UPDATE schedules s
LEFT JOIN (
  SELECT schedule_id, COUNT(*) AS reserved_cnt
  FROM booking_seats
  WHERE UPPER(COALESCE(status,'')) = 'ACTIVE'
  GROUP BY schedule_id
) b ON b.schedule_id = s.schedule_id
SET
  s.remain_count = GREATEST(0, s.total_count - COALESCE(b.reserved_cnt, 0)),
  s.status = CASE
      WHEN (s.total_count - COALESCE(b.reserved_cnt, 0)) <= 0 THEN 'CLOSED'
      ELSE 'OPEN'
  END
WHERE
  COALESCE(b.reserved_cnt, 0) <> 0
  OR s.remain_count <> GREATEST(0, s.total_count - COALESCE(b.reserved_cnt, 0))
  OR COALESCE(s.status, '') <> CASE
      WHEN (s.total_count - COALESCE(b.reserved_cnt, 0)) <= 0 THEN 'CLOSED'
      ELSE 'OPEN'
  END;

-- 콘서트 회차
UPDATE concert_shows cs
LEFT JOIN (
  SELECT show_id, COUNT(*) AS reserved_cnt
  FROM concert_booking_seats
  WHERE UPPER(COALESCE(status,'')) = 'ACTIVE'
  GROUP BY show_id
) b ON b.show_id = cs.show_id
SET
  cs.remain_count = GREATEST(0, cs.total_count - COALESCE(b.reserved_cnt, 0)),
  cs.status = CASE
      WHEN (cs.total_count - COALESCE(b.reserved_cnt, 0)) <= 0 THEN 'CLOSED'
      ELSE 'OPEN'
  END
WHERE
  COALESCE(b.reserved_cnt, 0) <> 0
  OR cs.remain_count <> GREATEST(0, cs.total_count - COALESCE(b.reserved_cnt, 0))
  OR COALESCE(cs.status, '') <> CASE
      WHEN (cs.total_count - COALESCE(b.reserved_cnt, 0)) <= 0 THEN 'CLOSED'
      ELSE 'OPEN'
  END;

