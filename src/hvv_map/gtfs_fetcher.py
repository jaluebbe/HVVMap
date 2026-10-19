"""Background loop: builds vehicle positions and reference layers purely
from the static HVV GTFS feed - no GTI credentials, no live API polling.

Positions are recomputed every POSITION_INTERVAL seconds by re-interpolating
the already-loaded schedule against a fresh timestamp - no disk I/O in the
hot loop, same idea as fetcher.py's _rebuild_positions(). Reference layers
(lines/stops) only change when the static feed itself is replaced, so
they're only rebuilt if routes.txt's mtime actually changed since the last
check, at most every REFERENCE_CHECK_INTERVAL seconds.
"""

import json
import os
import threading
import time

from hvv_map.gtfs_geojson import (
    build_lines_geojson,
    build_positions_geojson,
    build_stops_geojson,
)
from hvv_map.gtfs_bus_tracks import build_bus_tracks_geojson
from hvv_map.gtfs_schedule import load_schedule
from hvv_map.redis_client import get_redis_client

GTFS_DIR = os.environ.get("GTFS_DIR", "data/gtfs")
POSITION_INTERVAL = 1.0  # seconds between loop cycles (positions rebuild rate)
POSITIONS_TTL = 10  # seconds - stale data expires fast if the fetcher dies
REFERENCE_CHECK_INTERVAL = 1800.0  # how often to check the feed for changes
REFERENCE_TTL = 24 * 60 * 60  # seconds; rebuilt on feed change anyway

POSITIONS_KEY = "hvv:gtfs:positions"
LINES_KEY = "hvv:gtfs:reference_lines"
STOPS_KEY = "hvv:gtfs:reference_stops"
BUS_TRACKS_KEY = "hvv:gtfs:bus_tracks"  # developer map only, no TTL


def _store(redis_client, key: str, data: dict, ttl) -> None:
    payload = json.dumps({"fetched_at": int(time.time()), "data": data})
    redis_client.set(key, payload, ex=ttl)


def _routes_txt_mtime() -> float:
    return os.path.getmtime(os.path.join(GTFS_DIR, "routes.txt"))


_bus_tracks_lock = threading.Lock()


def _build_bus_tracks(redis_client, feed_mtime: float) -> None:
    """Store the bus tracks unless they already match this feed; the lock
    keeps a slow build from overlapping with the next check."""
    if not _bus_tracks_lock.acquire(blocking=False):
        return
    try:
        raw = redis_client.get(BUS_TRACKS_KEY)
        if raw and json.loads(raw).get("feed_mtime") == feed_mtime:
            return
        data = build_bus_tracks_geojson(GTFS_DIR)
        payload = {
            "fetched_at": int(time.time()),
            "feed_mtime": feed_mtime,
            "data": data,
        }
        redis_client.set(BUS_TRACKS_KEY, json.dumps(payload, separators=(",", ":")))
    finally:
        _bus_tracks_lock.release()


def _ensure_bus_tracks(redis_client) -> None:
    """Build the bus tracks in the background (a big feed takes a while)."""
    threading.Thread(
        target=_build_bus_tracks,
        args=(redis_client, _routes_txt_mtime()),
        daemon=True,
    ).start()


def _rebuild_reference(redis_client, schedule) -> None:
    _store(redis_client, LINES_KEY, build_lines_geojson(schedule), REFERENCE_TTL)
    _store(redis_client, STOPS_KEY, build_stops_geojson(schedule), REFERENCE_TTL)


def main() -> None:
    redis_client = get_redis_client()
    schedule = load_schedule(GTFS_DIR)
    _rebuild_reference(redis_client, schedule)
    _ensure_bus_tracks(redis_client)
    last_feed_mtime = _routes_txt_mtime()
    last_reference_check = time.time()

    while True:
        cycle_start = time.time()
        try:
            _store(
                redis_client,
                POSITIONS_KEY,
                build_positions_geojson(schedule, now_ts=int(cycle_start)),
                POSITIONS_TTL,
            )
            if cycle_start - last_reference_check >= REFERENCE_CHECK_INTERVAL:
                last_reference_check = cycle_start
                current_mtime = _routes_txt_mtime()
                if current_mtime != last_feed_mtime:
                    schedule = load_schedule(GTFS_DIR)
                    last_feed_mtime = current_mtime
                # Re-stored every check regardless of a feed change, so the TTL
                # keeps being renewed - otherwise it expires after REFERENCE_TTL
                # even though nothing was ever wrong with the data.
                _rebuild_reference(redis_client, schedule)
                _ensure_bus_tracks(redis_client)
        except Exception as e:  # keep the loop alive on transient errors
            print(f"[hvvmap-gtfs-fetcher] error: {e}", flush=True)

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, POSITION_INTERVAL - elapsed))


def fetch_once() -> None:
    """CLI: build all three layers once, store in Redis, then exit - for
    manual inspection via redis-cli."""
    redis_client = get_redis_client()
    schedule = load_schedule(GTFS_DIR)
    _rebuild_reference(redis_client, schedule)
    _build_bus_tracks(redis_client, _routes_txt_mtime())
    _store(
        redis_client,
        POSITIONS_KEY,
        build_positions_geojson(schedule, now_ts=int(time.time())),
        300,
    )
    print(
        f"Stored in Redis (keys: {POSITIONS_KEY}, {LINES_KEY}, {STOPS_KEY}, "
        f"{BUS_TRACKS_KEY})"
    )


if __name__ == "__main__":
    main()
