-- ticketing 기준데이터(Seed) 입력 (원본 3 tier 기준 복구/최신화)
-- - MySQL 5.7 호환(CTE/WITH RECURSIVE/윈도우 함수 사용 안 함)
-- - 운영자 제공 기준데이터: movies/theaters/halls/hall_seats/schedules/concerts/concert_shows
-- - 트랜잭션 데이터(booking/booking_seats/payment/users 생성 등)는 넣지 않음
-- - 재실행 시 중복 에러를 피하도록 INSERT IGNORE / NOT EXISTS / ON DUPLICATE KEY UPDATE 사용

USE ticketing;

-- =====================================================================
-- 1) 영화(1~10) + 더미(11~40) 복구
-- =====================================================================

INSERT INTO users (user_id, phone, name, created_at)
VALUES (1, '01012341234', '홍길동', NOW())
ON DUPLICATE KEY UPDATE
  phone = VALUES(phone),
  name = VALUES(name);

INSERT INTO movies (
  movie_id, title, genre, director, runtime_minutes,
  poster_url, main_poster_url, video_url, audience_count,
  release_date, synopsis, synopsis_line, status, hide,
  created_at, updated_at
)
VALUES
  (1, '왕과 사는 남자', '드라마', '장항준', 117, '/images/posters/king.jpg', '/images/posters/main_king.jpg', 'https://www.youtube.com/watch?v=9sxEZuJskvM', 3218700, '2026-03-28', '1457년 청령포, 역사가 지우려 했던 이야기', '“나는 이제 어디로 갑니까…”\n\n계유정난이 조선을 뒤흔들고\n어린 왕 이홍위는 왕위에서 쫓겨나 유배길에 오른다.\n\n“무슨 수를 쓰더라도 그 대감을 우리 광천골로 오게 해야지”\n\n한편, 강원도 영월 산골 마을 광천골의 촌장 엄흥도는\n먹고 살기 힘든 마을 사람들을 위해 청령포를 유배지로 만들기 위해 노력한다.\n그러나 촌장이 부푼 꿈으로 맞이한 이는 왕위에서 쫓겨난 이홍위였다.\n유배지를 지키는 보수주인으로서 그의 모든 일상을 감시해야만 하는 촌장은\n삶의 의지를 잃어버린 이홍위가 점점 신경 쓰이는데…\n\n1457년 청령포, 역사가 지우려 했던 이야기\n<왕과 사는 남자>', 'ACTIVE', 'N', '2026-04-03 21:01:25', '2026-04-04 16:51:31'),
  (2, '프로젝트 헤일메리', 'SF', '필로드', 156, '/images/posters/hail.jpg', '/images/posters/main_hail.jpg', 'https://www.youtube.com/watch?v=GC2SR2MGdck', 1543200, '2026-03-15', '죽어가는 태양, 종말 위기에 놓인 지구', '죽어가는 태양, 종말 위기에 놓인 지구\n인류의 운명을 건 단 하나의 미션\n그의 마지막 임무가 시작된다!\n\n눈을 떠보니 아득한 우주의 한가운데에서 깨어난 중학교 과학교사 ‘그레이스’는\n희미한 기억 속에서 자신이 죽어가는 태양으로부터 지구와 인류를 살릴 마지막 희망으로\n이곳에 왔다는 사실을 알게 된다.\n\n잃어버린 기억으로 인해 모든 것이 혼란스러운 상황에서\n‘그레이스’는 우연히 우주 한복판에서 같은 목적으로 온 뜻밖의 존재 ‘로키’를 만나게 되고\n‘그레이스’와 ‘로키’는 각 두 행성의 운명을 건 마지막 미션을 수행하러 떠나게 되는데…', 'ACTIVE', 'N', '2026-04-03 21:01:25', '2026-04-04 16:50:55'),
  (3, '호퍼스', '애니메이션', '다니엘 총', 104, '/images/posters/ho.jpg', '/images/posters/main_ho.jpg', 'https://www.youtube.com/watch?v=qNIYD3yzG-U', 2875400, '2026-02-20', '디즈니·픽사의 가장 사랑스러운 애니멀 어드벤처가 온다!', '비버 모드 ON!\n좋아, 자연스러웠어!\n\n동물과 자연을 사랑하는 소녀 ''메이블''은\n할머니와의 소중한 추억이 깃든 연못이 사라질 위기에 놓이자,\n이를 지키기 위한 방법을 찾기 위해 고군분투한다.\n \n어느 날, 사람의 의식을 동물 로봇으로 옮기는\n혁신적인 ''호핑'' 기술을 우연히 체험하게 된 ''메이블''!\n로봇 비버로 호핑한 그녀는 동물 세계에 잠입하게 된다.\n \n그 곳에서 열정적인 포유류의 왕 ''조지''를 비롯해\n다양한 개성을 지닌 동물들과 친구가 된 ''메이블''은\n연못을 지킬 수 있는 방법을 떠올리게 되고\n모두가 깜짝 놀랄 기상천외한 작전을 펼치게 되는데…\n \n2026년 3월, <아바타>만큼 흥미롭고 <주토피아> 뺨치게 귀여운\n디즈니·픽사의 가장 사랑스러운 애니멀 어드벤처가 온다!', 'ACTIVE', 'N', '2026-04-03 21:01:25', '2026-04-04 16:50:23'),
  (4, '위 리브 인 타임', '멜로/로맨스', '존 크로울리', 108, '/images/posters/we.jpg', '/images/posters/main_we.jpg', 'https://www.youtube.com/watch?v=qqlfn-T-OFA', 980300, '2026-04-08', '우리의 사랑은 함께한 시간에 영원히 남는다.', '본인의 레스토랑 오픈을 준비하며\n\n새로운 도약을 꿈꾸는 셰프 ‘알무트’.\n\n\n\n최근 이혼을 하면서\n\n삶의 한 챕터를 끝낸 ‘토비아스’.\n\n\n\n예기치 못한 만남을 계기로\n\n두 사람은 서로의 삶을 변화시키는\n\n잊지 못할 10년을 보낸다.\n\n\n\n우리의 사랑은 함께한 시간에 영원히 남는다.', 'ACTIVE', 'N', '2026-04-03 21:01:25', '2026-04-05 19:54:08'),
  (5, '쉘터', '액션', '릭 로먼 워', 107, '/images/posters/shelter.jpg', '/images/posters/dummy.jpg', 'https://www.youtube.com/watch?v=7Q9P8jyKczU', 0, '2026-04-15', '등대에 홀로 숨어살던 한 남자.', '등대에 홀로 숨어살던 한 남자.\n\n그 앞에 홀로 나타난 한 소녀.\n\n이제 소녀를 지키기 위해,\n그 남자의 숨겨왔던 액션 본능이 깨어난다!', 'ACTIVE', 'N', '2026-04-03 21:01:25', '2026-04-05 19:47:17'),
  (6, '네가 마지막으로 남긴 노래', '멜로/로맨스', '미키 타카히로', 117, '/images/posters/last_song.jpg', '/images/posters/dummy.jpg', 'https://www.youtube.com/watch?v=IKjpAIHqBPI', 42251, '2026-04-01', '시를 쓰는 소년과 노래로 세상을 그리는 소녀가 음악으로 이어지는 청춘 로맨스', '“나에게 가사를 써줄래?”\n\n유난히 눈에 띄지 않는 소년 ‘하루토’와\n유난히 빛나는 소녀 ‘아야네’.\n\n글을 읽고 쓰는 데 어려움이 있는 소녀를 대신해 소년은 시를 쓰고,\n그 시는 노래가 되어 소녀의 목소리로 세상에 울려 퍼진다.\n\n둘만의 비밀, 오직 둘만의 언어.\n\n말보다 음악이 먼저 닿은 순간,\n설렘으로 시작되는 가장 찬란한 청춘 로맨스!', 'ACTIVE', 'N', '2026-04-05 16:19:21', '2026-04-05 20:08:52'),
  (7, '살목지', '공포', '이상민', 95, '/images/posters/salmokji.jpg', '/images/posters/dummy.jpg', 'https://www.youtube.com/watch?v=jL2kpyq_ODk', 19156, '2026-04-08', '기이한 소문이 끊이지 않는 저수지에서 촬영팀이 정체불명의 존재와 마주하게 되는 공포 영화', '기이한 소문이 끊이지 않는 저수지 살목지의 로드뷰 화면에\n촬영한 적 없는 정체불명의 형체가 포착된다.\n오늘 안에 반드시 재촬영을 끝내야 하는 상황 속에\n살목지로 향한 PD ‘수인’(김혜윤)과 촬영팀.\n\n촬영이 시작되자 행방이 묘연했던 선배 ‘교식’(김준한)이 등장하고,\n설명되지 않는 일들이 연달아 벌어지며 촬영팀은 점점 아비규환에 빠진다.\n\n휘몰아치는 공포 속 ‘기태’(이종원)는 ‘수인’을 향해 내달리지만\n빠져나오려 할수록 이들은 점점 더 깊은 곳으로 끌려 들어가게 되는데…\n\n거긴, 절대 살아서는 못 나와', 'ACTIVE', 'N', '2026-04-05 16:19:21', '2026-04-05 20:08:52'),
  (8, '엔하이픈 - 이머전 인 시네마', '공연실황', '미상', 53, '/images/posters/enhypen_immersion.jpg', '/images/posters/dummy.jpg', 'https://www.youtube.com/watch?v=tb6xlBgBpug', 0, '2026-04-08', '엔하이픈의 몰입형 공연을 극장에서 만나는 스페셜 시네마', 'ENHYPEN과 함께하는 몰입의 세계가 더욱 강렬해진다!\n\n‘ENHYPEN VR CONCERT : IMMERSION’이 SCREENX와 4DX로 돌아왔다.\n\nSCREENX의 확장된 화면과 4DX의 생생한 효과를 통해, 눈앞 0cm에서 펼쳐지는 ENHYPEN의 무대를 극장에서 그대로 체험한다. 변화하는 공간 연출과 압도적인 퍼포먼스가 관객을 몰입의 세계로 이끈다.\n\n‘Bite Me’, ‘XO(Only If You Say Yes)’, 팬송 ‘Highway 1009’까지.\n오직 ENGENE를 위한 특별한 순간을 더 큰 스케일과 생동감으로 만난다.\n\n‘ENHYPEN : IMMERSION IN CINEMAS’\n지금, 또 하나의 몰입이 시작된다.', 'ACTIVE', 'N', '2026-04-05 16:21:38', '2026-04-05 20:08:52'),
  (9, '올란도', '드라마', '샐리 포터', 94, '/images/posters/orlando.jpg', '/images/posters/dummy.jpg', 'https://www.youtube.com/watch?v=D3seA01zVhQ', 0, '2026-04-08', '400년의 시간을 지나며 자신의 정체성을 찾아가는 올란도의 여정을 그린 영화', '16세기 영국, 엘리자베스 1세의 총애를 받던\n눈부신 미모의 귀공자 올란도(틸다 스윈튼)는\n영원히 시들지 말라는 명을 받는다\n\n사랑, 정치, 전쟁, 또다시 사랑...\n400년의 시간을 관통해\n남자도, 여자도 아닌 완전한 인간이 된 올란도는\n마침내 자신의 이야기를 쓰기 시작한다\n\n같은 사람이야, 변한 건 없어', 'ACTIVE', 'N', '2026-04-05 16:23:56', '2026-04-05 20:08:52'),
  (10, '내 이름은', '드라마', '정지영', 112, '/images/posters/my_name.jpg', '/images/posters/dummy.jpg', 'https://www.youtube.com/watch?v=P4Wv-zm-AJ0', 2057, '2026-04-15', '잃어버린 기억과 이름을 찾아가는 여정을 통해 제주 4·3의 상처를 그린 드라마', '“지독하게 아픈 봄이었수다, 우리 어멍의 1949년은”\n가슴에 묻은 78년의 약속, 이제야 부릅니다\n가장 아픈 비밀에서 가장 찬란한 진실이 된 ‘내 이름은’\n\n1998년의 봄, 촌스러운 이름 ‘영옥’이 인생 최대의 콤플렉스인 18세 소년. 어쩌다 서울에서 전학 온 경태의 눈에 들어 난생처음 반장 완장을 차지만, 결국 꼭두각시로 전락해 교실 안의 폭력을 무기력하게 방관하고 만다. 한편, 손자뻘인 아들 영옥을 홀로 억척스레 키워낸 어머니 정순에게도 지독하게 아팠던 1949년의 봄이 다시 찾아온다. 서울에서 새로 온 의사의 도움을 받아 까맣게 지워져 있던 어린 시절의 파편들을 하나둘 맞추기 시작하는 정순. 분홍색 선글라스를 끼고 하얀 차에 올라 제주의 곳곳을 누빌수록, 반세기 넘게 가슴 깊이 묻어두었던 그날의 슬픈 약속이 수면 위로 떠오르기 시작한다. 부끄러워 버리고 싶었던 소년의 이름과 온몸을 바쳐 지켜내야만 했던 어머니의 1949년. 기억조차 버거웠던 제주의 아픈 비밀이 78년의 시린 시간을 건너, 마침내 두 사람의 삶을 관통하는 가장 찬란한 진실이 되어 피어난다.', 'ACTIVE', 'N', '2026-04-05 16:25:55', '2026-04-05 20:08:52')
ON DUPLICATE KEY UPDATE
  title = VALUES(title),
  genre = VALUES(genre),
  director = VALUES(director),
  runtime_minutes = VALUES(runtime_minutes),
  poster_url = VALUES(poster_url),
  main_poster_url = VALUES(main_poster_url),
  video_url = VALUES(video_url),
  audience_count = VALUES(audience_count),
  release_date = VALUES(release_date),
  synopsis = VALUES(synopsis),
  synopsis_line = VALUES(synopsis_line),
  status = VALUES(status),
  hide = VALUES(hide),
  updated_at = VALUES(updated_at);

-- 더미데이터 11~40
-- - 영화목록(페이지네이션 확인용)에는 보여야 함: hide='N'
-- - 예매/상영중 목록에서만 제외: theaters_read.py 가 title/genre/director로 제외 처리
DROP PROCEDURE IF EXISTS seed_dummy_movies_11_40;
DELIMITER $$
CREATE PROCEDURE seed_dummy_movies_11_40()
BEGIN
  DECLARE i INT DEFAULT 11;
  WHILE i <= 40 DO
    INSERT INTO movies (
      movie_id, title, genre, director, runtime_minutes,
      poster_url, main_poster_url, video_url, audience_count,
      release_date, synopsis, synopsis_line, status, hide,
      created_at, updated_at
    )
    VALUES (
      i,
      CONCAT('더미데이터', i - 10),
      '더미',
      '더미',
      100,
      '/images/posters/dummy.jpg',
      '/images/posters/dummy.jpg',
      NULL,
      0,
      DATE_ADD('2026-04-05', INTERVAL (i - 11) DAY),
      CONCAT('더미데이터', i - 10),
      NULL,
      'ACTIVE',
      'N',
      NOW(),
      NOW()
    )
    ON DUPLICATE KEY UPDATE
      title = VALUES(title),
      genre = VALUES(genre),
      director = VALUES(director),
      runtime_minutes = VALUES(runtime_minutes),
      poster_url = VALUES(poster_url),
      main_poster_url = VALUES(main_poster_url),
      video_url = VALUES(video_url),
      audience_count = VALUES(audience_count),
      release_date = VALUES(release_date),
      synopsis = VALUES(synopsis),
      synopsis_line = VALUES(synopsis_line),
      status = VALUES(status),
      hide = VALUES(hide),
      updated_at = NOW();
    SET i = i + 1;
  END WHILE;
END$$
DELIMITER ;
CALL seed_dummy_movies_11_40();
DROP PROCEDURE IF EXISTS seed_dummy_movies_11_40;

-- =====================================================================
-- 2) 극장/상영관 + 좌석(3x10 전 상영관 보강)
-- =====================================================================

INSERT INTO theaters (theater_id, address, created_at, updated_at)
VALUES
  (1, '서울', NOW(), NOW()),
  (2, '경기', NOW(), NOW()),
  (3, '인천', NOW(), NOW()),
  (4, '대전', NOW(), NOW()),
  (5, '부산', NOW(), NOW())
ON DUPLICATE KEY UPDATE
  address = VALUES(address),
  updated_at = VALUES(updated_at);

INSERT INTO halls (theater_id, hall_name, total_seats, created_at, updated_at)
VALUES
  (1, 'A', 30, NOW(), NOW()),
  (1, 'B', 30, NOW(), NOW()),
  (2, 'A', 30, NOW(), NOW()),
  (2, 'B', 30, NOW(), NOW()),
  (3, 'A', 30, NOW(), NOW()),
  (3, 'B', 30, NOW(), NOW()),
  (4, 'A', 30, NOW(), NOW()),
  (4, 'B', 30, NOW(), NOW()),
  (5, 'A', 30, NOW(), NOW()),
  (5, 'B', 30, NOW(), NOW())
ON DUPLICATE KEY UPDATE
  total_seats = VALUES(total_seats),
  updated_at = VALUES(updated_at);

-- 전 상영관 3x10 좌석 채우기 (MySQL 5.7 호환)
INSERT IGNORE INTO hall_seats (hall_id, seat_row_no, seat_col_no, status, created_at)
SELECT
  h.hall_id,
  r.n AS seat_row_no,
  c.n AS seat_col_no,
  'ACTIVE' AS status,
  NOW() AS created_at
FROM halls h
CROSS JOIN (SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3) r
CROSS JOIN (
  SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5
  UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9 UNION ALL SELECT 10
) c;

-- =====================================================================
-- 3) 스케줄: 2026-04-07 ~ 2026-05-30 "꽉 채움" (영화 1~10 골고루)
--    - MySQL 5.7 호환: 루프 기반
--    - 요구사항: hall_id(상영관) 기준으로 "상영시간이 안겹치게" 편성
--      -> 시작시간만 저장되므로, 러닝타임 + 정리시간(15분) 간격으로 회차 생성
-- =====================================================================

DROP PROCEDURE IF EXISTS seed_one_schedule;
DELIMITER $$
CREATE PROCEDURE seed_one_schedule(p_movie_id BIGINT, p_hall_id BIGINT, p_show_dt DATETIME)
BEGIN
  INSERT INTO schedules (
    movie_id, hall_id, show_date,
    total_count, remain_count, status,
    created_at, updated_at
  )
  SELECT
    p_movie_id,
    p_hall_id,
    p_show_dt,
    COALESCE(h.total_seats, 30),
    COALESCE(h.total_seats, 30),
    'OPEN',
    NOW(),
    NOW()
  FROM halls h
  WHERE h.hall_id = p_hall_id
  ON DUPLICATE KEY UPDATE
    movie_id = VALUES(movie_id),
    total_count = VALUES(total_count),
    remain_count = VALUES(remain_count),
    status = VALUES(status),
    updated_at = NOW();
END$$
DELIMITER ;

DROP PROCEDURE IF EXISTS seed_schedules_full_until_0530;
DELIMITER $$
CREATE PROCEDURE seed_schedules_full_until_0530()
BEGIN
  DECLARE d DATE DEFAULT '2026-04-07';
  DECLARE end_d DATE DEFAULT '2026-05-30';
  DECLARE done INT DEFAULT 0;
  DECLARE v_hall_id BIGINT;
  DECLARE slot_idx INT;
  DECLARE v_movie_id BIGINT DEFAULT 1;
  DECLARE v_runtime_min INT DEFAULT 117;
  DECLARE v_gap_min INT DEFAULT 15;
  DECLARE v_step_min INT DEFAULT 132;
  DECLARE v_show_dt DATETIME;

  DECLARE cur CURSOR FOR
    SELECT hall_id FROM halls ORDER BY hall_id ASC;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;

  -- 영화 1번(왕과 사는 남자) 러닝타임을 우선 사용 (없으면 기본 117분)
  SELECT COALESCE(runtime_minutes, 117) INTO v_runtime_min
  FROM movies
  WHERE movie_id = 1
  LIMIT 1;
  SET v_step_min = v_runtime_min + v_gap_min;

  WHILE d <= end_d DO
    SET done = 0;
    OPEN cur;

    read_loop: LOOP
      FETCH cur INTO v_hall_id;
      IF done = 1 THEN
        LEAVE read_loop;
      END IF;

      SET slot_idx = 0;
      WHILE slot_idx < 7 DO
        -- 09:00부터 (러닝타임+15분) 간격으로 7회차 생성
        -- 예: 117분 + 15분 = 132분 간격 -> 09:00, 11:12, 13:24, 15:36, 17:48, 20:00, 22:12
        SET v_show_dt = DATE_ADD(CONCAT(d, ' 09:00:00'), INTERVAL (slot_idx * v_step_min) MINUTE);

        -- 시연용으로 영화 1~10이 골고루 섞이게 편성 (일자/상영관/회차에 따라 라운드로빈)
        SET v_movie_id = 1 + MOD((slot_idx + CAST(v_hall_id AS SIGNED) + DATEDIFF(d, '2026-04-07')), 10);
        CALL seed_one_schedule(v_movie_id, v_hall_id, v_show_dt);

        SET slot_idx = slot_idx + 1;
      END WHILE;
    END LOOP;

    CLOSE cur;
    SET d = DATE_ADD(d, INTERVAL 1 DAY);
  END WHILE;
END$$
DELIMITER ;

CALL seed_schedules_full_until_0530();
DROP PROCEDURE IF EXISTS seed_schedules_full_until_0530;
DROP PROCEDURE IF EXISTS seed_one_schedule;

-- =====================================================================
-- 4) 콘서트/뮤지컬 시드(소규모) + 5만석 시드 복구
-- =====================================================================

INSERT INTO concerts (
  title, category, genre, venue_summary, poster_url, runtime_minutes,
  synopsis, synopsis_line, status, hide
)
SELECT
  '2026 오페라 갈라 콘서트 - 봄(Spring)', 'CONCERT', '페스티벌', '올림픽공원 체조경기장', '/images/posters/concert3.jpg', 150,
  '왕초보를 위한 천원의 오페라', '야외 페스티벌, 단 하루.', 'ACTIVE', 'N'
WHERE NOT EXISTS (SELECT 1 FROM concerts WHERE title='2026 오페라 갈라 콘서트 - 봄(Spring)' LIMIT 1);

INSERT INTO concerts (
  title, category, genre, venue_summary, poster_url, runtime_minutes,
  synopsis, synopsis_line, status, hide
)
SELECT
  '2026 뮤지컬 콘서트 <더 미션 : K>', 'MUSICAL', '뮤지컬', '샤롯데씨어터', '/images/posters/concert2.jpg', 140,
  '감동적인 스토리와 라이브 밴드가 어우러진 창작 뮤지컬.', '당신의 밤을 비추는 이야기.', 'ACTIVE', 'N'
WHERE NOT EXISTS (SELECT 1 FROM concerts WHERE title='2026 뮤지컬 콘서트 <더 미션 : K>' LIMIT 1);

SET @cid1 := (SELECT concert_id FROM concerts WHERE title='2026 오페라 갈라 콘서트 - 봄(Spring)' ORDER BY concert_id ASC LIMIT 1);
SET @cid2 := (SELECT concert_id FROM concerts WHERE title='2026 뮤지컬 콘서트 <더 미션 : K> ' ORDER BY concert_id ASC LIMIT 1);

INSERT INTO concert_shows (
  concert_id, show_date, venue_name, venue_address, hall_name,
  seat_rows, seat_cols, total_count, remain_count, price, status,
  created_at, updated_at
)
SELECT * FROM (
  SELECT @cid1 AS concert_id, '2026-05-10 18:00:00' AS show_date, '올림픽공원 체조경기장' AS venue_name, '서울 송파구 올림픽로 424' AS venue_address, '스탠딩 A' AS hall_name,
         5 AS seat_rows, 10 AS seat_cols, 50 AS total_count, 50 AS remain_count, 150000 AS price, 'OPEN' AS status, NOW() AS created_at, NOW() AS updated_at
  UNION ALL
  SELECT @cid1, '2026-05-11 15:00:00', '올림픽공원 체조경기장', '서울 송파구 올림픽로 424', '스탠딩 B',
         5, 10, 50, 50, 150000, 'OPEN', NOW(), NOW()
  UNION ALL
  SELECT @cid2, '2026-06-01 19:30:00', '샤롯데씨어터', '서울 송파구 올림픽로 240', '1층',
         5, 10, 50, 50, 99000, 'OPEN', NOW(), NOW()
  UNION ALL
  SELECT @cid2, '2026-06-02 14:00:00', '샤롯데씨어터', '서울 송파구 올림픽로 240', '1층',
         5, 10, 50, 50, 99000, 'OPEN', NOW(), NOW()
) AS v
WHERE v.concert_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM concert_shows cs
    WHERE cs.concert_id = v.concert_id
      AND cs.show_date = v.show_date
      AND cs.hall_name = v.hall_name
  );

-- 5만석(200 x 250) 콘서트/회차 복구 (원본 seed_concert_large_50000.sql 기반)
INSERT INTO concerts
  (title, category, genre, venue_summary, poster_url, runtime_minutes, synopsis, synopsis_line, status, hide)
SELECT
   'BTS WORLD TOUR ''ARIRANG''', 'CONCERT', '페스티벌', '고양종합운동장 주경기장 · 스탠딩 A', '/images/posters/concert1.jpg', 140,
  '2026년 4월 9일부터 시작된 방탄소년단의 단독 스타디움 투어', '2026년 4월 9일부터 시작된 방탄소년단의 단독 스타디움 투어', 'ACTIVE', 'N'
WHERE NOT EXISTS (SELECT 1 FROM concerts WHERE title= 'BTS WORLD TOUR ''ARIRANG''' LIMIT 1);

SET @cid_big := (SELECT concert_id FROM concerts WHERE title= 'BTS WORLD TOUR ''ARIRANG''' ORDER BY concert_id ASC LIMIT 1);

INSERT INTO concert_shows
  (concert_id, show_date, venue_name, venue_address, hall_name, seat_rows, seat_cols, total_count, remain_count, price, status, created_at, updated_at)
SELECT
  @cid_big,
  '2026-05-10 18:00:00',
  '고양종합운동장 주경기장',
  '서울 송파구 올림픽로 424',
  '스탠딩 A',
  200,
  250,
  50000,
  50000,
  120000,
  'OPEN',
  NOW(),
  NOW()
WHERE @cid_big IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM concert_shows cs
    WHERE cs.concert_id = @cid_big
      AND cs.show_date = '2026-05-10 18:00:00'
      AND cs.hall_name = '스탠딩 A'
  );

