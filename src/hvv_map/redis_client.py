"""Shared Redis connection helper.

Host from REDIS_HOST env var (set by Docker Compose), falls back to
127.0.0.1 for local testing against a Redis port exposed on the host.
DB index from REDIS_DB, default 0 - tests override this (see conftest.py)
so they never touch production data on a shared Redis instance.
"""

import os

import redis


def get_redis_client() -> redis.Redis:
    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    db = int(os.environ.get("REDIS_DB", "0"))
    return redis.Redis(host=host, port=6379, db=db, decode_responses=True)
