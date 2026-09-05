"""Shared Redis connection helper.

Host from REDIS_HOST env var (set by Docker Compose), falls back to
127.0.0.1 for local testing against a Redis port exposed on the host.
"""

import os

import redis


def get_redis_client() -> redis.Redis:
    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    return redis.Redis(host=host, port=6379, decode_responses=True)
