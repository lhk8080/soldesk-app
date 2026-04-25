"""
DB 연결.

- Writer(본 DB): 쓰기·트랜잭션·가입 직전 중복 검사 등 정합성이 중요한 읽기.
- Reader(조회): Redis **read 캐시**를 채우는 경로·일반 조회 API. 리더 없음/장애 시 Writer로 폴백.

NOTE: DB_READ_REPLICA_ENABLED=false(기본) 이면 리더 호스트를 보지 않고 항상 writer 만 사용한다.
리플리카 도입 후 true 로 켜고 DB_READER_HOST 를 분리하면 리더 우선·실패 시 writer 폴백.
"""
import logging

import pymysql

from config import (
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_READ_REPLICA_ENABLED,
    DB_READER_HOST,
    DB_USER,
)

log = logging.getLogger(__name__)


def _connect(host: str):
    return pymysql.connect(
        host=host,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def get_db_connection():
    """Writer(본 DB). 쓰기 모듈·정합성 중시 읽기용."""
    return _connect(DB_HOST)


def get_db_read_connection():
    """
    조회 전용 연결. Redis 웜업·캐시 미스·read API 조회가 이쪽을 쓴다.

    - DB_READ_REPLICA_ENABLED=false: 항상 writer (R/O 없을 때, 지금 시점 기본).
    - true: reader 우선, 연결 실패 시 writer 폴백.
    """
    writer_host = (DB_HOST or "").strip()

    if not DB_READ_REPLICA_ENABLED:
        return _connect(writer_host)

    reader_host = (DB_READER_HOST or "").strip() or writer_host

    if not reader_host or reader_host == writer_host:
        return _connect(writer_host)

    try:
        return _connect(reader_host)
    except (pymysql.err.OperationalError, pymysql.err.InterfaceError, OSError) as exc:
        log.warning(
            "db read endpoint unreachable (%s), falling back to writer (%s): %s",
            reader_host,
            writer_host,
            exc,
        )
        return _connect(writer_host)
