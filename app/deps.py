"""Connections to Postgres and Redis.

Each one is created once per process, on first use, not at import time.
That keeps the app importable (and testable) when neither service is running.
"""

import os
from functools import lru_cache

import redis
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


@lru_cache(maxsize=1)
def pg_pool() -> ConnectionPool:
    """Postgres connection pool, shared by LangGraph's checkpointer and the health check."""
    return ConnectionPool(
        conninfo=_required("DATABASE_URL"),
        min_size=1,
        max_size=5,
        open=True,
        # LangGraph's PostgresSaver expects autocommit, dict rows and no prepared statements.
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )


@lru_cache(maxsize=1)
def redis_client() -> redis.Redis:
    """Redis client with short timeouts, so a slow Redis can't hang a request."""
    return redis.Redis.from_url(
        _required("REDIS_URL"),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
