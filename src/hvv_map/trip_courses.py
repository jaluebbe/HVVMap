"""departureCourse per upcoming trip: the full stop sequence with
planned times and delays, stored under hvv:trips:courses for trips.py."""

import json
import time

import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hvv_map.departures import REDIS_ANCHOR_KEY, REDIS_BY_SERVICE_KEY, berlin_now
from hvv_map.departures_tree import parse_anchor
from hvv_map.gti_client import GtiClient
from hvv_map.redis_client import get_redis_client
from hvv_map.departures_live import REDIS_LIVE_KEY
from hvv_map.trips import (
    _plan_trips,
    alias_course_key,
    build_trip_table,
    course_key,
    trip_end,
)

BERLIN_TZ = ZoneInfo("Europe/Berlin")
REDIS_COURSES_KEY = "hvv:trips:courses"
COURSES_TTL = 3 * 24 * 3600
COURSE_LOOKAHEAD_MINUTES = 60  # fetch trips with a stop up to this far ahead
COURSE_MAX_CALLS = 30  # per run, at 1 req/s
COURSE_KEEP_SECONDS = 2 * 24 * 3600
COURSE_RETRY_SECONDS = 600  # a failed fetch is not retried before this
COURSE_REFRESH_SECONDS = 300  # running trips refetch their course after this
COURSE_RUNNING_SLACK_MINUTES = 2
COURSE_TIME_TOLERANCE_MINUTES = 10  # requested time vs. the course's own times


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


def _request_time(request: dict) -> datetime:
    return datetime.fromisoformat(request["time"]).replace(tzinfo=None)


def course_fits_request(stops: list[dict], request: dict) -> bool:
    """GTI may answer a serviceId with another run of the same day; the course
    must pass the requested time within the tolerance."""
    wanted = _request_time(request)
    tolerance = timedelta(minutes=COURSE_TIME_TOLERANCE_MINUTES)
    return any(
        abs(datetime.fromisoformat(stop[key]) - wanted) <= tolerance
        for stop in stops
        for key in ("dep", "arr")
        if stop.get(key)
    )


def course_request(live: dict, service_id: int, station_id: str | None = None):
    """departureCourse request for the live departure with this serviceId
    (at station_id if it appears at several stations), or None if absent."""
    anchor = live["anchor"]
    start = datetime.strptime(f"{anchor['date']} {anchor['time']}", "%Y-%m-%d %H:%M:%S")
    for dep in live["departures"]:
        if dep["serviceId"] != service_id:
            continue
        if station_id and dep["station"]["id"] != station_id:
            continue
        when = start.replace(tzinfo=BERLIN_TZ) + timedelta(minutes=dep["timeOffset"])
        return {
            "station": {"type": "STATION", "name": dep["station"]["combinedName"]},
            "time": when.strftime("%Y-%m-%dT%H:%M:%S.000%z"),
            "lineId": dep["line"]["dlid"],
            "serviceId": service_id,
            "showPath": False,
        }
    return None


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


def _is_current(entry: dict | None, running: bool = False) -> bool:
    """No fetch needed: a failed fetch counts until its retry time, a running
    trip's course until it is older than COURSE_REFRESH_SECONDS."""
    if entry is None:
        return False
    age = time.time() - entry["fetched_at"]
    if entry["stops"] is None:
        return age < COURSE_RETRY_SECONDS
    return not running or age < COURSE_REFRESH_SECONDS


def _renumbered(row: dict) -> bool:
    """A matched trip whose live serviceId differs from its plan's: GTI
    renumbers services after plan changes, so the plan id may name another run."""
    return bool(row["live_ids"]) and row["service_id"] not in row["live_ids"]


def _is_current_by(entry: dict | None, service_id: str) -> bool:
    """A course fetched with this very serviceId that needs no refetch."""
    return (
        entry is not None
        and entry.get("service_id") == service_id
        and _is_current(entry)
    )


def _is_running(trip: dict, now: datetime, course: dict | None = None) -> bool:
    first, last = trip["stops"][0]["planned"], trip_end(trip, course)
    slack = timedelta(minutes=COURSE_RUNNING_SLACK_MINUTES)
    return first <= now <= last + slack


def due_trips(trips: list[dict], cached: dict, now: datetime) -> list[dict]:
    """Trips with a stop in [now, now + lookahead] whose course is missing or,
    while the trip runs, outdated. Missing first, then oldest, then soonest."""
    end = now + timedelta(minutes=COURSE_LOOKAHEAD_MINUTES)
    due = [
        trip
        for trip in trips
        if not _is_current(
            cached.get(course_key(trip)),
            _is_running(trip, now, cached.get(course_key(trip))),
        )
        and any(now <= stop["planned"] <= end for stop in trip["stops"])
    ]
    due.sort(
        key=lambda trip: (
            (cached.get(course_key(trip)) or {"fetched_at": 0})["fetched_at"],
            trip["stops"][0]["planned"],
        )
    )
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

    # (cache key, request) - live-only entries (alias ids) first
    jobs = []
    renumbered = []
    queued = set()
    by_id = {trip["id"]: trip for trip in trips}
    raw_live = redis_client.get(REDIS_LIVE_KEY)
    if raw_live:
        live = json.loads(raw_live)["data"]
        table = build_trip_table(json.loads(raw_plan)["data"], plan_anchor, live)
        for row in table["trips"]:
            if row["live_only"]:
                request = course_request(live, int(row["service_id"]))
                key = row["course_key"]
                if request and not _is_current(cached.get(key), running=True):
                    jobs.append((key, request))
                    queued.add(key)
            elif _renumbered(row):
                key, live_id = row["course_key"], row["live_ids"][0]
                request = course_request(live, int(live_id))
                if request:
                    queued.add(key)  # never refetched by the stale plan id
                    if not _is_current_by(cached.get(key), live_id):
                        renumbered.append((key, request))
            for alias in row["aliases"]:
                key = alias_course_key(alias["service_id"])
                request = course_request(
                    live, int(alias["service_id"]), alias["station_id"]
                )
                if request and not _is_current(cached.get(key), running=True):
                    jobs.append((key, request))
                    queued.add(key)
                    # refresh the split train's own course alongside its twin
                    main = by_id.get(row["id"])
                    main_request = _query(main) if main else None
                    if main_request and course_key(main) not in queued:
                        jobs.append((course_key(main), main_request))
                        queued.add(course_key(main))
    renumbered.sort(key=lambda job: abs(_request_time(job[1]) - now))
    live_jobs = len(jobs)
    jobs += renumbered
    for trip in due_trips(trips, cached, now):
        request = _query(trip)
        if request and course_key(trip) not in queued:
            jobs.append((course_key(trip), request))

    if jobs:
        print(
            f"[trip_courses] {len(jobs)} jobs ({live_jobs} live-only/alias, "
            f"{len(renumbered)} renumbered), running {COURSE_MAX_CALLS}",
            flush=True,
        )
    fetched = failed = 0
    for key, request in jobs[:COURSE_MAX_CALLS]:
        try:
            response = client.send("departureCourse", request)
        except requests.RequestException as error:
            print(f"[trip_courses] {key}: {error}", flush=True)
            response = {}
        time.sleep(1)  # 1 req/s rate limit across all GTI endpoints
        stops = parse_course(response)
        if stops is not None and not course_fits_request(stops, request):
            print(
                f"[trip_courses] {key}: course does not match the request", flush=True
            )
            stops = None
        cached[key] = {
            "fetched_at": int(time.time()),
            "stops": stops,
            "service_id": str(request["serviceId"]),
        }
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
