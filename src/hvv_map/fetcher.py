"""Background poller: fetches GTI API data and caches it in Redis.

Rate limit: max 1 request/second across ALL endpoints (see GTI docs, 1.10).
Each loop cycle runs at most one job, and a job that makes several requests
sleeps 1 s between them. Vehicle positions are not fetched: trip_positions.py
computes them every second from the departure data stored here.

Jobs, each on its own interval:
- getAnnouncements -> hvv:announcements, hvv:disruptions,
  hvv:announcement_stations
- listLines -> hvv:sublines (always a full, non-incremental call)
- listStations -> hvv:stations (needed for disruptions and positions)
- departure snapshots: the 5-minute realtime window (hvv:departures:live)
  and the daily plan snapshots (hvv:departures:*, they skip themselves once
  current)
- departureCourse for running trips -> hvv:trips:courses
- subline tracks -> gti_shapes database
"""

import json
import threading
import time

from hvv_map.disruptions import (
    build_disruptions_geojson,
    extract_announcement_station_names,
)
from hvv_map.departures import run_daily_snapshots, snapshots_stale
from hvv_map.departures_live import run_live_snapshot
from hvv_map.trip_courses import run_trip_courses
from hvv_map.trip_positions import run_loop as run_trip_positions_loop
from hvv_map.gti_client import GtiClient
from hvv_map.gti_shapes import open_db as open_shapes_db
from hvv_map.gti_shapes import run_update as run_gti_shapes_update
from hvv_map.lines import ANNOUNCEMENT_FILTER_NAMES, update_sublines_cache
from hvv_map.redis_client import get_redis_client
from hvv_map.stations import by_id, fetch_stations, load_stations, store_stations

LOOP_INTERVAL = 1.0  # seconds between loop cycles
ANNOUNCEMENTS_INTERVAL = 600.0  # seconds between getAnnouncements calls (10 min)
STATIONS_INTERVAL = 6 * 3600.0  # seconds between listStations calls (6 h)
SUBLINES_CACHE_INTERVAL = 3600.0  # seconds between hvv:sublines updates (1h)
LIVE_DEPARTURES_INTERVAL = 240.0  # seconds between live departure snapshots (4 min)
LIVE_RETRY_SECONDS = 30.0  # retry of a live snapshot that could not be stored
TRIP_COURSES_INTERVAL = 120.0  # seconds between trip course fetches (2 min)
GTI_SHAPES_INTERVAL = 1800.0  # seconds between subline/track updates (30 min)
DAILY_DEPARTURES_INTERVAL = 3600.0  # seconds between daily snapshot checks (1h)
DAILY_DEPARTURES_RETRY = 60.0  # seconds between retries while the plan is stale
ANNOUNCEMENTS_TTL = 7200  # seconds - generous headroom above refresh interval

redis_client = get_redis_client()
gti = GtiClient()


def _store(key: str, data: dict, ttl: int) -> None:
    payload = json.dumps({"fetched_at": int(time.time()), "data": data})
    redis_client.set(key, payload, ex=ttl)


def fetch_announcements() -> None:
    request = {
        "full": True,
        "names": ANNOUNCEMENT_FILTER_NAMES,
    }
    response = gti.send("getAnnouncements", request)
    _store("hvv:announcements", response, ANNOUNCEMENTS_TTL)

    stations = by_id(
        load_stations(redis_client)
    )  # empty dict if hvvmap-stations hasn't run yet
    disruptions = build_disruptions_geojson(response, stations)
    _store("hvv:disruptions", disruptions, ANNOUNCEMENTS_TTL)

    # Extra departureList lookup targets, beyond the fixed anchor stations -
    # raw station names only, no id resolution, no reason stored.
    station_names = extract_announcement_station_names(response)
    _store("hvv:announcement_stations", station_names, ANNOUNCEMENTS_TTL)


def fetch_stations_catalog() -> None:
    store_stations(redis_client, fetch_stations(gti))


def fetch_live_departures_snapshot() -> bool:
    """True if a snapshot was stored."""
    data = run_live_snapshot(gti, redis_client)
    for error in data["errors"]:
        print(f"[hvvmap-fetcher] live departures: {error}", flush=True)
    if not data["complete"]:
        print("[hvvmap-fetcher] live departures: kept the old snapshot", flush=True)
    return data["complete"]


def fetch_trip_courses() -> None:
    fetched, failed = run_trip_courses(gti, redis_client)
    if failed:
        print(f"[hvvmap-fetcher] trip courses: {failed} failed", flush=True)


def fetch_gti_shapes() -> None:
    conn = open_shapes_db()
    try:
        summary, derived = run_gti_shapes_update(gti, redis_client, conn)
    finally:
        conn.close()
    done = summary["resolved"] + (derived or {}).get("derived", 0)
    failed = summary["failed"] + (derived or {}).get("failed", 0)
    if done or failed:
        print(
            f"[hvvmap-fetcher] gti shapes: {done} resolved, {failed} failed", flush=True
        )


def fetch_daily_departures() -> None:
    run_daily_snapshots(gti, redis_client)


def fetch_once_announcements() -> None:
    """CLI: fetch announcements once, store in Redis, then exit."""
    fetch_announcements()
    print(
        "Stored in Redis (keys: hvv:announcements, hvv:disruptions, "
        "hvv:announcement_stations)"
    )


def main() -> None:
    # own thread: GTI jobs below can block the loop for tens of seconds
    threading.Thread(target=run_trip_positions_loop, daemon=True).start()
    last_announcements_fetch = 0.0
    last_stations = 0.0
    last_sublines_cache_update = 0.0
    last_live_departures = 0.0
    last_daily_departures = 0.0
    last_trip_courses = 0.0
    last_gti_shapes = 0.0
    while True:
        cycle_start = time.time()
        try:
            if cycle_start - last_stations >= STATIONS_INTERVAL:
                last_stations = cycle_start
                fetch_stations_catalog()
            elif cycle_start - last_announcements_fetch >= ANNOUNCEMENTS_INTERVAL:
                fetch_announcements()
                last_announcements_fetch = cycle_start
            elif cycle_start - last_sublines_cache_update >= SUBLINES_CACHE_INTERVAL:
                update_sublines_cache(gti, redis_client)
                last_sublines_cache_update = cycle_start
            elif cycle_start - last_daily_departures >= DAILY_DEPARTURES_INTERVAL or (
                cycle_start - last_daily_departures >= DAILY_DEPARTURES_RETRY
                and snapshots_stale(redis_client)
            ):
                last_daily_departures = cycle_start
                fetch_daily_departures()
            elif cycle_start - last_live_departures >= LIVE_DEPARTURES_INTERVAL:
                last_live_departures = cycle_start
                if not fetch_live_departures_snapshot():
                    # retry soon instead of after the full interval
                    last_live_departures -= (
                        LIVE_DEPARTURES_INTERVAL - LIVE_RETRY_SECONDS
                    )
            elif cycle_start - last_trip_courses >= TRIP_COURSES_INTERVAL:
                last_trip_courses = cycle_start
                fetch_trip_courses()
            elif cycle_start - last_gti_shapes >= GTI_SHAPES_INTERVAL:
                last_gti_shapes = cycle_start
                fetch_gti_shapes()
        except Exception as e:  # keep the loop alive on transient API/network errors
            print(f"[hvvmap-fetcher] error: {e}", flush=True)

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, LOOP_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
