import pymysql, os
conn = pymysql.connect(host=os.environ['DB_READER_HOST'], user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'], db='ticketing')
cur = conn.cursor()

print('=== 영화 목록 ===')
cur.execute('SELECT movie_id, title, genre, director, runtime_minutes FROM movies LIMIT 20')
for r in cur.fetchall(): print(r)
print()

print('=== 극장/상영관 ===')
cur.execute('SELECT t.theater_id, t.name, h.hall_id, h.name, h.total_seats FROM theaters t JOIN halls h ON t.theater_id = h.theater_id')
for r in cur.fetchall(): print(r)
print()

print('=== 상영 스케줄 ===')
cur.execute('SELECT s.schedule_id, m.title, s.show_date, s.show_time, s.available_seats FROM schedules s JOIN movies m ON s.movie_id = m.movie_id ORDER BY s.show_date DESC LIMIT 20')
for r in cur.fetchall(): print(r)
print()

print('=== 영화 예매 현황 ===')
cur.execute('SELECT b.booking_id, b.status, b.total_price, b.created_at FROM booking b ORDER BY b.created_at DESC LIMIT 20')
for r in cur.fetchall(): print(r)
print()

print('=== 공연 목록 ===')
cur.execute('SELECT concert_id, title, venue, genre FROM concerts LIMIT 20')
for r in cur.fetchall(): print(r)
print()

print('=== 공연 회차 ===')
cur.execute('SELECT cs.show_id, c.title, cs.show_date, cs.show_time, cs.total_seats, cs.available_seats FROM concert_shows cs JOIN concerts c ON cs.concert_id = c.concert_id ORDER BY cs.show_date DESC LIMIT 20')
for r in cur.fetchall(): print(r)
print()

print('=== 공연 예매 현황 ===')
cur.execute('SELECT cb.booking_id, cb.status, cb.total_price, cb.created_at FROM concert_booking cb ORDER BY cb.created_at DESC LIMIT 20')
for r in cur.fetchall(): print(r)

conn.close()
