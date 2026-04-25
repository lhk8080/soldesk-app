-- Kubernetes/RDS 기준 스키마 생성 (DB_NAME=ticketing)
-- - 이 파일은 "테이블/제약/인덱스 생성"만 담당합니다.
-- - 사용자/권한(root, GRANT 등)은 Terraform RDS 마스터 계정 + K8s Secret에서 관리합니다.
-- - 테스트/운영 데이터 삭제(TRUNCATE/DELETE) 로직은 포함하지 않습니다.
-- - 재실행해도 에러가 나지 않도록 테이블은 IF NOT EXISTS 로 생성합니다.
-- - 인덱스는 CREATE INDEX IF NOT EXISTS(MySQL 8.0.29+) 대신, information_schema로 존재 확인 후 조건부 생성합니다.

CREATE DATABASE IF NOT EXISTS ticketing
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_general_ci;

USE ticketing;

SET default_storage_engine = INNODB;

-- =====================================================================
-- 1) 영화 예매 도메인
-- =====================================================================

CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  phone VARCHAR(20) NULL,
  email VARCHAR(255) NULL,
  cognito_sub VARCHAR(255) UNIQUE,
  -- Cognito 전환 후 실로그인에는 미사용. 형 Insert.sql 더미 유저 시딩 호환을 위해 컬럼만 유지.
  password_hash VARCHAR(255) NULL,
  name VARCHAR(50) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS movies (
  movie_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(100) NOT NULL,
  genre VARCHAR(50) NULL,
  director VARCHAR(100) NULL,
  runtime_minutes INT NOT NULL DEFAULT 0 COMMENT '상영시간(분)',
  poster_url VARCHAR(255) NULL,
  main_poster_url VARCHAR(255) NULL,
  video_url VARCHAR(255) NULL COMMENT '동영상 URL 주소',
  audience_count BIGINT NOT NULL DEFAULT 0 COMMENT '누적 관객수',
  release_date DATE NULL,
  synopsis TEXT NULL,
  synopsis_line TEXT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
  hide CHAR(1) NOT NULL DEFAULT 'N' COMMENT '숨김여부(Y/N)',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS theaters (
  theater_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  address VARCHAR(255) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS halls (
  hall_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  theater_id BIGINT NOT NULL,
  hall_name VARCHAR(20) NOT NULL COMMENT 'A관, B관',
  total_seats INT NOT NULL DEFAULT 30,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_halls_theater
    FOREIGN KEY (theater_id) REFERENCES theaters(theater_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT uq_halls_theater_name UNIQUE (theater_id, hall_name),
  CONSTRAINT chk_halls_total_seats CHECK (total_seats > 0)
);

CREATE TABLE IF NOT EXISTS hall_seats (
  seat_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  hall_id BIGINT NOT NULL,
  seat_row_no INT NOT NULL COMMENT '1,2,3...',
  seat_col_no INT NOT NULL COMMENT '1~10',
  status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_hall_seats_hall
    FOREIGN KEY (hall_id) REFERENCES halls(hall_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT uq_hall_seats_position UNIQUE (hall_id, seat_row_no, seat_col_no),
  CONSTRAINT chk_hall_seats_row_no CHECK (seat_row_no > 0),
  CONSTRAINT chk_hall_seats_col_no CHECK (seat_col_no > 0)
);

CREATE TABLE IF NOT EXISTS schedules (
  schedule_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  movie_id BIGINT NOT NULL,
  hall_id BIGINT NOT NULL,
  show_date DATETIME NOT NULL,
  total_count INT NOT NULL,
  /* 런타임 잔여는 booking_seats ACTIVE 건수로 유도; 컬럼은 제약·시드·수동 동기화용 */
  remain_count INT NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_schedules_movie
    FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_schedules_hall
    FOREIGN KEY (hall_id) REFERENCES halls(hall_id)
    ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT chk_schedules_total_count CHECK (total_count >= 0),
  CONSTRAINT chk_schedules_remain_count CHECK (remain_count >= 0),
  CONSTRAINT chk_schedules_valid_count CHECK (remain_count <= total_count)
);

CREATE TABLE IF NOT EXISTS booking (
  booking_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  booking_code VARCHAR(50) NULL UNIQUE,
  user_id BIGINT NOT NULL,
  schedule_id BIGINT NOT NULL,
  reg_count INT NOT NULL,
  book_status VARCHAR(20) NOT NULL DEFAULT 'HOLD',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_booking_user
    FOREIGN KEY (user_id) REFERENCES users(user_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_booking_schedule
    FOREIGN KEY (schedule_id) REFERENCES schedules(schedule_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT chk_booking_reg_count CHECK (reg_count > 0)
);

CREATE TABLE IF NOT EXISTS booking_seats (
  booking_seat_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  booking_id BIGINT NOT NULL,
  schedule_id BIGINT NOT NULL,
  seat_id BIGINT NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- ACTIVE만 (schedule_id, seat_id) 유니크 보장 (취소/환불 이력은 status 변경으로 보존)
  active_schedule_id BIGINT
    GENERATED ALWAYS AS (CASE WHEN status = 'ACTIVE' THEN schedule_id ELSE NULL END) VIRTUAL,
  active_seat_id BIGINT
    GENERATED ALWAYS AS (CASE WHEN status = 'ACTIVE' THEN seat_id ELSE NULL END) VIRTUAL,
  CONSTRAINT fk_booking_seats_booking
    FOREIGN KEY (booking_id) REFERENCES booking(booking_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_booking_seats_schedule
    FOREIGN KEY (schedule_id) REFERENCES schedules(schedule_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_booking_seats_seat
    FOREIGN KEY (seat_id) REFERENCES hall_seats(seat_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  UNIQUE INDEX uq_booking_seats_active_seat (active_schedule_id, active_seat_id)
);

CREATE TABLE IF NOT EXISTS payment (
  payment_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  booking_id BIGINT NOT NULL,
  pay_yn CHAR(1) NOT NULL DEFAULT 'N',
  paid_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_payment_booking
    FOREIGN KEY (booking_id) REFERENCES booking(booking_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT uq_payment_booking UNIQUE (booking_id),
  CONSTRAINT chk_payment_pay_yn CHECK (pay_yn IN ('Y', 'N'))
);

-- =====================================================================
-- 2) 콘서트/뮤지컬 도메인
-- =====================================================================

CREATE TABLE IF NOT EXISTS concerts (
  concert_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(200) NOT NULL,
  category VARCHAR(30) NULL COMMENT 'CONCERT, MUSICAL',
  genre VARCHAR(50) NULL,
  venue_summary VARCHAR(255) NULL,
  poster_url VARCHAR(255) NULL,
  runtime_minutes INT NOT NULL DEFAULT 120,
  synopsis TEXT NULL,
  synopsis_line VARCHAR(500) NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
  hide CHAR(1) NOT NULL DEFAULT 'N',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS concert_shows (
  show_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  concert_id BIGINT NOT NULL,
  show_date DATETIME NOT NULL,
  venue_name VARCHAR(200) NOT NULL,
  venue_address VARCHAR(255) NOT NULL,
  hall_name VARCHAR(80) NOT NULL DEFAULT '홀',
  seat_rows INT NOT NULL,
  seat_cols INT NOT NULL,
  total_count INT NOT NULL,
  /* 런타임 잔여는 concert_booking_seats ACTIVE 건수로 유도; 컬럼은 제약·시드·수동 동기화용 */
  remain_count INT NOT NULL,
  price INT NOT NULL DEFAULT 120000,
  status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_concert_shows_concert
    FOREIGN KEY (concert_id) REFERENCES concerts(concert_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT chk_concert_shows_counts
    CHECK (remain_count >= 0 AND total_count >= 0 AND remain_count <= total_count),
  CONSTRAINT chk_concert_shows_seat_dim
    CHECK (seat_rows > 0 AND seat_cols > 0 AND total_count = seat_rows * seat_cols)
);

CREATE TABLE IF NOT EXISTS concert_booking (
  booking_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  booking_code VARCHAR(50) NULL,
  user_id BIGINT NOT NULL,
  show_id BIGINT NOT NULL,
  reg_count INT NOT NULL,
  book_status VARCHAR(20) NOT NULL DEFAULT 'PAID',
  sqs_booking_ref VARCHAR(64) NULL COMMENT '비동기 예매 멱등 키(write-api booking_ref UUID)',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_concert_booking_user
    FOREIGN KEY (user_id) REFERENCES users(user_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_concert_booking_show
    FOREIGN KEY (show_id) REFERENCES concert_shows(show_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT chk_concert_booking_reg CHECK (reg_count > 0),
  UNIQUE INDEX uq_concert_booking_code (booking_code),
  UNIQUE INDEX uq_concert_booking_sqs_ref (sqs_booking_ref)
);

CREATE TABLE IF NOT EXISTS concert_booking_seats (
  booking_seat_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  booking_id BIGINT NOT NULL,
  show_id BIGINT NOT NULL,
  seat_row_no INT NOT NULL,
  seat_col_no INT NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  active_show_id BIGINT
    GENERATED ALWAYS AS (CASE WHEN status = 'ACTIVE' THEN show_id ELSE NULL END) VIRTUAL,
  active_row_no INT
    GENERATED ALWAYS AS (CASE WHEN status = 'ACTIVE' THEN seat_row_no ELSE NULL END) VIRTUAL,
  active_col_no INT
    GENERATED ALWAYS AS (CASE WHEN status = 'ACTIVE' THEN seat_col_no ELSE NULL END) VIRTUAL,
  CONSTRAINT fk_cbs_booking
    FOREIGN KEY (booking_id) REFERENCES concert_booking(booking_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_cbs_show
    FOREIGN KEY (show_id) REFERENCES concert_shows(show_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT chk_cbs_row CHECK (seat_row_no > 0),
  CONSTRAINT chk_cbs_col CHECK (seat_col_no > 0),
  UNIQUE INDEX uq_concert_booking_seats_active_seat (active_show_id, active_row_no, active_col_no)
);

CREATE TABLE IF NOT EXISTS concert_payment (
  payment_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  booking_id BIGINT NOT NULL,
  pay_yn CHAR(1) NOT NULL DEFAULT 'Y',
  paid_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_concert_payment_booking
    FOREIGN KEY (booking_id) REFERENCES concert_booking(booking_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT uq_concert_payment_booking UNIQUE (booking_id),
  CONSTRAINT chk_concert_pay_yn CHECK (pay_yn IN ('Y', 'N'))
);

-- =====================================================================
-- 3) 인덱스(조건부 생성, 중복 실행 시에도 1061 방지)
-- =====================================================================

SET @__db := DATABASE();

-- movies
SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='movies' AND index_name='idx_movies_title');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_movies_title ON movies(title)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='movies' AND index_name='idx_movies_status');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_movies_status ON movies(status)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

-- halls / hall_seats
SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='halls' AND index_name='idx_halls_theater_id');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_halls_theater_id ON halls(theater_id)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='hall_seats' AND index_name='idx_hall_seats_hall_id');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_hall_seats_hall_id ON hall_seats(hall_id)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='hall_seats' AND index_name='idx_hall_seats_status');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_hall_seats_status ON hall_seats(status)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

-- schedules
SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='schedules' AND index_name='idx_schedules_movie_id');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_schedules_movie_id ON schedules(movie_id)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='schedules' AND index_name='idx_schedules_hall_id');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_schedules_hall_id ON schedules(hall_id)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

-- schedules unique (hall_id, show_date) for deterministic upsert/seed
SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='schedules' AND index_name='uq_schedules_hall_show_date');
SET @__sql := IF(@__n=0,'CREATE UNIQUE INDEX uq_schedules_hall_show_date ON schedules(hall_id, show_date)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='schedules' AND index_name='idx_schedules_show_date');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_schedules_show_date ON schedules(show_date)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='schedules' AND index_name='idx_schedules_status');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_schedules_status ON schedules(status)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

-- booking
SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='booking' AND index_name='idx_booking_user_id');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_booking_user_id ON booking(user_id)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='booking' AND index_name='idx_booking_schedule_id');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_booking_schedule_id ON booking(schedule_id)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='booking' AND index_name='idx_booking_status');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_booking_status ON booking(book_status)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

-- booking_seats
SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='booking_seats' AND index_name='idx_booking_seats_booking_id');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_booking_seats_booking_id ON booking_seats(booking_id)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='booking_seats' AND index_name='idx_booking_seats_schedule_id');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_booking_seats_schedule_id ON booking_seats(schedule_id)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='booking_seats' AND index_name='idx_booking_seats_seat_id');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_booking_seats_seat_id ON booking_seats(seat_id)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

SET @__n := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=@__db AND table_name='booking_seats' AND index_name='idx_booking_seats_status');
SET @__sql := IF(@__n=0,'CREATE INDEX idx_booking_seats_status ON booking_seats(status)','SELECT 1');
PREPARE __stmt FROM @__sql; EXECUTE __stmt; DEALLOCATE PREPARE __stmt;

