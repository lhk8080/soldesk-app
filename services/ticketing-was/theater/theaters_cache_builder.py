"""
극장 캐시 리빌드 — 웜업과 동일하게만 동작.

실제 Redis 키는 theaters_read 의 부트스트랩·예매 상세(v6)만 사용한다.
(구 theaters:list:v1 / theater:detail:*:v1 는 사용처가 없어 제거됨.)
"""


def rebuild_theaters_cache():
    from theater.theaters_read import warmup_theaters_booking_caches

    return warmup_theaters_booking_caches()
