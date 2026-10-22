"""departureCourse per live serviceId: the full stop sequence with planned
times and delays, stored under hvv:trips:courses (key "live:<serviceId>")
for trips.py."""

import json
import time
from collections import defaultdict

import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hvv_map.course_log import KIND_TRIP, call_entry, log_calls
from hvv_map.departures import berlin_now
from hvv_map.gti_client import GtiClient
from hvv_map.redis_client import get_redis_client
from hvv_map.vehicle_sightings import course_sightings, record
from hvv_map.departures_live import REDIS_LIVE_KEY
from hvv_map.gtilive_geojson import mode_for_line
from hvv_map.trips import _is_trip_line, alias_course_key

BERLIN_TZ = ZoneInfo("Europe/Berlin")
REDIS_COURSES_KEY = "hvv:trips:courses"
COURSES_TTL = 3 * 24 * 3600
COURSE_MAX_CALLS = 30  # per run, at 1 req/s
COURSE_KEEP_SECONDS = 2 * 24 * 3600
COURSE_RETRY_SECONDS = 600  # a failed fetch is not retried before this
COURSE_RETRY_LIVE_SECONDS = 120  # retry of a failed fetch ...
COURSE_RETRY_LIVE_ATTEMPTS = 3  # ... for this many tries, then COURSE_RETRY_SECONDS
COURSE_REFRESH_SECONDS = 300  # running trips refetch their course after this
COURSE_RUNNING_SLACK_MINUTES = 2
COURSE_DELAYED_SECONDS = 120  # delayed trains are refreshed first
COURSE_END_SKIP_MINUTES = 5  # a course ending sooner is not refreshed
COURSE_TIME_TOLERANCE_MINUTES = 10  # requested time vs. the course's own times


def _parse_time(value: str | None) -> str | None:
    """Local wall-clock ISO string without offset."""
    if not value:
        return None
    return (
        datetime.fromisoformat(value).replace(tzinfo=None).isoformat(timespec="minutes")
    )


def parse_course(response: dict) -> list[dict] | None:
    """[{id, name, dep, arr, dep_delay, arr_delay, platform, cancelled}] per
    station of the course, or None without courseElements. cancelled: the
    train does not stop there (realtime)."""
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
                "arr": (
                    _parse_time(elements[index - 1].get("arrTime")) if index else None
                ),
                "dep_delay": element.get("depDelay"),
                "arr_delay": elements[index - 1].get("arrDelay") if index else None,
                "platform": element.get("fromRealtimePlatform")
                or element.get("fromPlatform"),
                "cancelled": bool(element.get("fromCancelled")),
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
            "cancelled": bool(last.get("toCancelled")),
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


def _dep_request(start: datetime, dep: dict) -> dict:
    when = start.replace(tzinfo=BERLIN_TZ) + timedelta(minutes=dep["timeOffset"])
    return {
        "station": {"type": "STATION", "name": dep["station"]["combinedName"]},
        "time": when.strftime("%Y-%m-%dT%H:%M:%S.000%z"),
        "lineId": dep["line"]["dlid"],
        "serviceId": dep["serviceId"],
        "showPath": False,
    }


def _live_start(live: dict) -> datetime:
    anchor = live["anchor"]
    return datetime.strptime(f"{anchor['date']} {anchor['time']}", "%Y-%m-%d %H:%M:%S")


def course_request(live: dict, service_id: int, station_id: str | None = None):
    """departureCourse request for the live departure with this serviceId
    (at station_id if it appears at several stations), or None if absent."""
    for dep in live["departures"]:
        if dep["serviceId"] != service_id:
            continue
        if station_id and dep["station"]["id"] != station_id:
            continue
        return _dep_request(_live_start(live), dep)
    return None


def _retry_due(entry: dict, now_ts: float) -> bool:
    """A failed fetch is retried soon a few times, then rarely."""
    age = now_ts - entry["fetched_at"]
    if entry.get("attempts", 1) < COURSE_RETRY_LIVE_ATTEMPTS:
        return age >= COURSE_RETRY_LIVE_SECONDS
    return age >= COURSE_RETRY_SECONDS


def _is_running(entry: dict, now: datetime) -> bool:
    """The course's times span `now` (with some slack at the end)."""
    times = [
        datetime.fromisoformat(stop[key])
        for stop in entry["stops"]
        for key in ("dep", "arr")
        if stop.get(key)
    ]
    slack = timedelta(minutes=COURSE_RUNNING_SLACK_MINUTES)
    return bool(times) and min(times) <= now <= max(times) + slack


# Reasons of a course request, see course_jobs
REASON_NEW = "new"
REASON_OTHER_RUN = "other_run"  # cached course is another run of the id
REASON_RETRY = "retry"
REASON_STATION = "station"  # live departure at a station the course lacks
REASON_BETWEEN = "between"  # running between anchors, no live departure
REASON_REGIONAL = "regional"
REASON_DELAYED = "delayed"
REASON_AGE = "age"


def course_jobs(live: dict | None, cached: dict, now: datetime, now_ts: float):
    """(cache key, request, reason) per live serviceId whose course is missing, does
    not run at the departure's time (ids repeat daily), lacks a station the
    id departs from (refetched at that station) or, while running, is older
    than COURSE_REFRESH_SECONDS. These first; of the old ones, trips without
    a live departure (between anchors) come first, then delayed and regional
    trains, then the oldest; a course ending soon is not refreshed. Cancelled
    departures are ignored."""
    by_id: dict[str, list] = defaultdict(list)
    if live:
        for dep in live["departures"]:
            if (
                _is_trip_line(dep.get("line") or {})
                and dep.get("timeOffset") is not None
                and not dep.get("cancelled")
            ):
                by_id[str(dep["serviceId"])].append(dep)
    start = _live_start(live) if live else None
    urgent, stale = [], []
    for service_id, deps in by_id.items():
        deps.sort(key=lambda dep: dep["timeOffset"])
        key = alias_course_key(service_id)
        entry = cached.get(key)
        request = _dep_request(start, deps[0])
        if entry is None:
            urgent.append((key, request, REASON_NEW))
        elif entry["stops"] is None:
            if _retry_due(entry, now_ts):
                urgent.append((key, request, REASON_RETRY))
        elif not course_fits_request(entry["stops"], request):
            urgent.append((key, request, REASON_OTHER_RUN))
        else:
            known = {stop["id"] for stop in entry["stops"]}
            outside = [
                dep
                for dep in deps
                if dep["station"]["id"] not in known
                and dep["station"]["id"] != entry.get("station_id")
            ]
            if outside:
                urgent.append((key, _dep_request(start, outside[0]), REASON_STATION))
            elif now_ts - entry["fetched_at"] >= COURSE_REFRESH_SECONDS:
                regional = mode_for_line(deps[0].get("line") or {}) == "R"
                stale.append(
                    (True, entry, regional, key, entry.get("request") or request)
                )
    # running trips between their anchors: refresh with the stored request
    for key, entry in cached.items():
        if (
            key.startswith(alias_course_key(""))
            and key[len(alias_course_key("")) :] not in by_id
            and entry.get("stops")
            and entry.get("request")
            and now_ts - entry["fetched_at"] >= COURSE_REFRESH_SECONDS
            and _is_running(entry, now)
        ):
            stale.append(
                (False, entry, entry.get("mode") == "R", key, entry["request"])
            )
    refresh = []
    for live_seen, entry, regional, key, request in stale:
        if _ends_soon(entry, now):
            continue
        delayed = _delayed(entry, now)
        reason = (
            REASON_BETWEEN
            if not live_seen
            else (
                REASON_REGIONAL
                if regional
                else REASON_DELAYED if delayed else REASON_AGE
            )
        )
        order = (live_seen, not (regional or delayed), entry["fetched_at"])
        refresh.append((order, key, request, reason))
    refresh.sort(key=lambda job: job[0])
    return urgent + [job[1:] for job in refresh]


def _stop_times(entry: dict) -> list[tuple[datetime, int | None]]:
    """(time, delay) per stop of a course."""
    result = []
    for stop in entry["stops"]:
        key = "dep" if stop.get("dep") else "arr"
        if stop.get(key):
            result.append((datetime.fromisoformat(stop[key]), stop.get(f"{key}_delay")))
    return result


def _delayed(entry: dict, now: datetime) -> bool:
    """A stop still ahead is delayed by COURSE_DELAYED_SECONDS or more."""
    return any(
        at >= now and (delay or 0) >= COURSE_DELAYED_SECONDS
        for at, delay in _stop_times(entry)
    )


def _ends_soon(entry: dict, now: datetime) -> bool:
    times = _stop_times(entry)
    end = timedelta(minutes=COURSE_END_SKIP_MINUTES)
    return bool(times) and times[-1][0] < now + end


def delay_change(old: list[dict] | None, new: list[dict] | None) -> int | None:
    """Largest delay difference in seconds at the stops both courses share
    (same station and planned time), None without a comparison."""
    if not old or not new:
        return None

    def delays(stops):
        return {
            (stop["id"], stop.get("dep") or stop.get("arr")): (
                stop.get("dep_delay")
                if stop.get("dep_delay") is not None
                else stop.get("arr_delay")
            )
            for stop in stops
        }

    before, after = delays(old), delays(new)
    changes = [
        abs(after[key] - before[key])
        for key in before.keys() & after.keys()
        if before[key] is not None and after[key] is not None
    ]
    return max(changes) if changes else None


def run_trip_courses(client: GtiClient, redis_client, now: datetime | None = None):
    """Fetch up to COURSE_MAX_CALLS courses by live serviceId; returns
    (fetched, failed)."""
    now = now or berlin_now()
    now_ts = time.time()
    raw = redis_client.get(REDIS_COURSES_KEY)
    cached = json.loads(raw)["data"] if raw else {}
    keep_after = now_ts - COURSE_KEEP_SECONDS
    cached = {k: v for k, v in cached.items() if v["fetched_at"] >= keep_after}
    raw_live = redis_client.get(REDIS_LIVE_KEY)
    live = json.loads(raw_live)["data"] if raw_live else None
    jobs = course_jobs(live, cached, now, now_ts)

    if len(jobs) > COURSE_MAX_CALLS:
        print(
            f"[trip_courses] {len(jobs)} jobs, running {COURSE_MAX_CALLS}", flush=True
        )
    fetched = failed = 0
    sightings = []
    by_name = _station_ids(live)
    modes = {
        str(dep["serviceId"]): mode_for_line(dep.get("line") or {})
        for dep in (live or {}).get("departures", [])
    }
    calls = []
    for key, request, reason in jobs[:COURSE_MAX_CALLS]:
        started = time.time()
        result = "ok"
        try:
            response = client.send("departureCourse", request)
        except requests.RequestException as error:
            print(f"[trip_courses] {key} {request['lineId']}: {error}", flush=True)
            response = {}
            status = getattr(getattr(error, "response", None), "status_code", None)
            result = f"HTTP {status}" if status else type(error).__name__
        stops = parse_course(response)
        if stops is None and result == "ok":
            result = "no course"
        if stops is not None and not course_fits_request(stops, request):
            print(
                f"[trip_courses] {key} {request['lineId']}: "
                "course does not match the request",
                flush=True,
            )
            stops = None
            result = "other run"
        if stops is not None:
            sightings += course_sightings(request, response)
        previous = cached.get(key) or {}
        calls.append(
            call_entry(
                KIND_TRIP,
                request,
                result,
                started,
                stops=len(stops or []),
                reason=reason,
                delay_change=delay_change(previous.get("stops"), stops),
            )
        )
        time.sleep(1)  # 1 req/s rate limit across all GTI endpoints
        attempts = 0 if stops else previous.get("attempts", 0) + 1
        cached[key] = {
            "fetched_at": int(time.time()),
            "attempts": attempts,
            "stops": stops,
            "service_id": str(request["serviceId"]),
            "station_id": by_name.get(request["station"]["name"]),
            "request": request,
            "mode": modes.get(str(request["serviceId"])) or previous.get("mode"),
            "vehicles": (response.get("vehicles") or []) if stops else [],
        }
        if stops is None:
            failed += 1
        else:
            fetched += 1

    payload = json.dumps({"fetched_at": int(time.time()), "data": cached})
    redis_client.set(REDIS_COURSES_KEY, payload, ex=COURSES_TTL)
    record(redis_client, sightings)
    log_calls(redis_client, calls)
    return fetched, failed


def _station_ids(live: dict | None) -> dict[str, str]:
    """Station id by the combined name used in course requests."""
    return {
        dep["station"]["combinedName"]: dep["station"]["id"]
        for dep in (live or {}).get("departures", [])
    }


def main() -> None:
    """CLI: fetch the missing upcoming trip courses once."""
    fetched, failed = run_trip_courses(GtiClient(), get_redis_client())
    print(f"{fetched} courses fetched, {failed} failed")
