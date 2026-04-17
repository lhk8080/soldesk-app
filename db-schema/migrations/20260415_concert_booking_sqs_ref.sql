-- 콘서트 비동기 예매 멱등: 동일 booking_ref 재전달 시 두 번째 INSERT가 좌석 유니크에만 걸리며
-- 폴링 결과가 DUPLICATE_SEAT로 덮어씌워지는 문제를 막기 위한 컬럼/유니크 인덱스.
-- 기존 DB 적용: mysql ... < db-schema/migrations/20260415_concert_booking_sqs_ref.sql

ALTER TABLE concert_booking
  ADD COLUMN sqs_booking_ref VARCHAR(64) NULL
    COMMENT '비동기 예매 멱등 키(write-api booking_ref UUID)'
    AFTER book_status;

CREATE UNIQUE INDEX uq_concert_booking_sqs_ref ON concert_booking (sqs_booking_ref);
