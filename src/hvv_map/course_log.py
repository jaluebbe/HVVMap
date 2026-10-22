"""Short log of all departureCourse calls (trip courses, subline tracks,
CLI) under hvv:courses:log, newest first, for static/dev_courses.html."""

import json
import time

import redis

REDIS_COURSE_LOG_KEY = "hvv:courses:log"
COURSE_LOG_MAX = 5000  # entries kept, a few hours of trip courses

KIND_TRIP = "trip"
KIND_TRACK = "track"
KIND_MANUAL = "manual"


def _line_name(line_id: str) -> str:
    """Line name from a dlid, e.g. de:hvv:S1: -> S1."""
    parts = line_id.split(":")
    return parts[2] if len(parts) > 2 and parts[2] else line_id


def call_entry(
    kind: str,
    request: dict,
    result: str,
    started: float,
    stops: int = 0,
    reason: str | None = None,
    line: str | None = None,
    delay_change: int | None = None,
    ms: int | None = None,
) -> dict:
    """One log entry; started is the time.time() before the call, ms its
    duration (default: until now)."""
    return {
        "at": int(started),
        "kind": kind,
        "reason": reason,
        "line": line or _line_name(request.get("lineId") or ""),
        "service_id": str(request.get("serviceId")),
        "station": (request.get("station") or {}).get("name"),
        "time": request.get("time"),
        "result": result,
        "stops": stops,
        "ms": ms if ms is not None else int((time.time() - started) * 1000),
        "delay_change": delay_change,
    }


def log_calls(redis_client, entries: list[dict]) -> None:
    """Prepend entries (oldest first in the list) and cut the log."""
    if not entries:
        return
    try:
        redis_client.lpush(REDIS_COURSE_LOG_KEY, *(json.dumps(e) for e in entries))
        redis_client.ltrim(REDIS_COURSE_LOG_KEY, 0, COURSE_LOG_MAX - 1)
    except redis.RedisError as error:
        print(f"[course_log] {error}", flush=True)


def read_calls(redis_client, limit: int = COURSE_LOG_MAX) -> list[dict]:
    return [
        json.loads(raw)
        for raw in redis_client.lrange(REDIS_COURSE_LOG_KEY, 0, limit - 1)
    ]
