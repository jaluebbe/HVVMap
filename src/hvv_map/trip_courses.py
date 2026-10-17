"""departureCourse per upcoming U-Bahn trip: the full stop sequence with
planned times and delays, stored under hvv:trips:courses for trips.py."""

import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hvv_map.departures import REDIS_ANCHOR_KEY, REDIS_BY_SERVICE_KEY, berlin_now
from hvv_map.departures_tree import parse_anchor
from hvv_map.gti_client import GtiClient
from hvv_map.redis_client import get_redis_client
from hvv_map.trips import _plan_trips, course_key

BERLIN_TZ = ZoneInfo("Europe/Berlin")
REDIS_COURSES_KEY = "hvv:trips:courses"
COURSES_TTL = 3 * 24 * 3600
COURSE_LOOKAHEAD_MINUTES = 60  # fetch trips with a stop up to this far ahead
COURSE_MAX_CALLS = 15  # per run, at 1 req/s
COURSE_KEEP_SECONDS = 2 * 24 * 3600
COURSE_RETRY_SECONDS = 600  # a failed fetch is not retried before this


def _parse_time(value: str | None) -> str | None:
    """Local wall-clock ISO string without offset."""
    if not value:
        return None
    return (
        datetime.fromisoformat(value).replace(tzinfo=None).isoformat(timespec="minutes")
    )


def parse_course(response: dict) -> list[dict] | None:
    """[{id, name, dep, arr, dep_delay, arr_delay, platform}] per station of
    the course, or None without courseElements."""
    elements = response.get("courseElements") or []
    if not elements:
        return None
    stops = []
    for index, element in enumerate(elements):
        origin = element.get("fromStation") or {}
        stops.append(
            {
                "id": origin.get("id"),
                "name": origin.get("name"),
                "dep": _parse_time(element.get("depTime")),
                "arr": _parse_time(elements[index - 1].get("arrTime"))
                if index
                else None,
                "dep_delay": element.get("depDelay"),
                "arr_delay": elements[index - 1].get("arrDelay") if index else None,
                "platform": element.get("fromRealtimePlatform")
                or element.get("fromPlatform"),
            }
        )
    last, final = elements[-1], elements[-1].get("toStation") or {}
    stops.append(
        {
            "id": final.get("id"),
            "name": final.get("name"),
            "dep": None,
            "arr": _parse_time(last.get("arrTime")),
            "dep_delay": None,
            "arr_delay": last.get("arrDelay"),
            "platform": last.get("toRealtimePlatform") or last.get("toPlatform"),
        }
    )
    return stops


def _query(trip: dict) -> dict | None:
    first = trip["stops"][0]
    line_id = (trip["line_obj"] or {}).get("dlid")
    if not first["station"] or not line_id:
        return None
    return {
        "station": {"type": "STATION", "name": first["station"]},
        "time": first["planned"]
        .replace(tzinfo=BERLIN_TZ)
        .strftime("%Y-%m-%dT%H:%M:%S.000%z"),
        "lineId": line_id,
        "serviceId": int(trip["service_id"]),
        "showPath": False,
    }


def _is_cached(entry: dict | None) -> bool:
    """A course is cached; a failed fetch (stops None) counts until retry."""
    if entry is None:
        return False
    return entry["stops"] is not None or (
        time.time() - entry["fetched_at"] < COURSE_RETRY_SECONDS
    )


def due_trips(trips: list[dict], cached: dict, now: datetime) -> list[dict]:
    """Trips with a stop in [now, now + lookahead] and no usable cache entry,
    soonest first."""
    end = now + timedelta(minutes=COURSE_LOOKAHEAD_MINUTES)
    due = [
        trip
        for trip in trips
        if not _is_cached(cached.get(course_key(trip)))
        and any(now <= stop["planned"] <= end for stop in trip["stops"])
    ]
    due.sort(key=lambda trip: trip["stops"][0]["planned"])
    return due


def run_trip_courses(client: GtiClient, redis_client, now: datetime | None = None):
    """Fetch up to COURSE_MAX_CALLS missing courses; returns (fetched, failed)."""
    now = now or berlin_now()
    raw_plan = redis_client.get(REDIS_BY_SERVICE_KEY)
    raw_anchor = redis_client.get(REDIS_ANCHOR_KEY)
    if not raw_plan or not raw_anchor:
        return 0, 0
    plan_anchor = parse_anchor(json.loads(raw_anchor)["data"])
    trips = _plan_trips(json.loads(raw_plan)["data"], plan_anchor)

    raw = redis_client.get(REDIS_COURSES_KEY)
    cached = json.loads(raw)["data"] if raw else {}
    keep_after = time.time() - COURSE_KEEP_SECONDS
    cached = {k: v for k, v in cached.items() if v["fetched_at"] >= keep_after}

    fetched = failed = 0
    for trip in due_trips(trips, cached, now)[:COURSE_MAX_CALLS]:
        request = _query(trip)
        if request is None:
            continue
        response = client.send("departureCourse", request)
        time.sleep(1)  # 1 req/s rate limit across all GTI endpoints
        stops = parse_course(response)
        cached[course_key(trip)] = {"fetched_at": int(time.time()), "stops": stops}
        if stops is None:
            failed += 1
        else:
            fetched += 1

    payload = json.dumps({"fetched_at": int(time.time()), "data": cached})
    redis_client.set(REDIS_COURSES_KEY, payload, ex=COURSES_TTL)
    return fetched, failed


def main() -> None:
    """CLI: fetch the missing upcoming trip courses once."""
    fetched, failed = run_trip_courses(GtiClient(), get_redis_client())
    print(f"{fetched} courses fetched, {failed} failed")
