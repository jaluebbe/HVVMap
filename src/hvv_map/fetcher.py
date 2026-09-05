"""Background poller: fetches GTI API data and caches it in Redis.

Rate limit: max 1 request/second across ALL endpoints (see GTI docs, 1.10).
Actual getVehicleMap calls happen at most every VEHICLE_MAP_FETCH_INTERVAL;
hvv:positions itself is rebuilt every loop cycle by re-interpolating the
last fetched data against a fresh timestamp, so movement stays smooth
without polling the API every second. getAnnouncements periodically takes
a cycle instead of adding to it, keeping the combined rate within 1 req/s.

getVehicleMap alternates realtime=True/False on successive fetches. Each
variant is stored under its own key (hvv:vehiclemap:realtime /
hvv:vehiclemap:no_realtime) and published to a same-named channel.
hvv:positions is built from the no_realtime variant only.

hvv:reference_stops/hvv:reference_lines are rebuilt periodically here
(every REFERENCE_REBUILD_INTERVAL), replacing a vehicle-map cycle, since
segment_cache keeps growing between rebuilds. The rebuild needs two API
calls (listLines, listStations), split across two consecutive cycles.
"""

import json
import time

from hvv_map.disruptions import build_disruptions_geojson
from hvv_map.geojson import build_positions_geojson, mode_for_journey
from hvv_map.gti_client import GtiClient
from hvv_map.lines import (
    ANNOUNCEMENT_FILTER_NAMES,
    REPLACEMENT_BUS_MODES,
    fetch_sublines,
)
from hvv_map.redis_client import get_redis_client
from hvv_map.reference_geojson import finish_reference_rebuild
from hvv_map.segment_cache import (
    get_or_fetch_tracks,
    open_cache,
    record_line_usage,
    record_station_seen,
)
from hvv_map.stations import by_id, load_stations

VEHICLE_MAP_INTERVAL = 1.0  # seconds between loop cycles (positions rebuild rate)
VEHICLE_MAP_FETCH_INTERVAL = 5.0  # seconds between actual getVehicleMap API calls
ANNOUNCEMENTS_INTERVAL = 600.0  # seconds between getAnnouncements calls (10 min)
REFERENCE_REBUILD_INTERVAL = 1800.0  # seconds between reference layer rebuilds (30 min)
VEHICLE_MAP_TTL = 10  # seconds - stale data expires fast if fetcher dies
ANNOUNCEMENTS_TTL = 7200  # seconds - generous headroom above refresh interval

VEHICLE_TYPES = ["U_BAHN", "S_BAHN", "A_BAHN", "SCHIFF", "REGIONALBUS"]
# Wide window, not just now..now+10: a journey's full segment chain arrives
# in one response, giving _select_segment() something to pick from even
# during a station dwell. Narrow windows would only catch a single segment
# and miss the handoff between them.
PRE_DEPARTURE_SECONDS = 60  # how early before a first departure a vehicle appears
POST_ARRIVAL_SECONDS = 30  # how long after a last arrival a vehicle stays visible
BOUNDING_BOX = {
    "lowerLeft": {"x": 9.45, "y": 53.43, "type": "EPSG_4326"},
    "upperRight": {"x": 10.35, "y": 54.08, "type": "EPSG_4326"},
}

# getVehicleMap has no line-name filter (unlike getAnnouncements' "names"),
# so REGIONALBUS pulls in all regional buses, not just our replacement
# services. Filtered client-side against REPLACEMENT_BUS_MODES (lines.py).

redis_client = get_redis_client()
gti = GtiClient()
segment_cache_conn = open_cache()

_last_no_realtime_response: dict | None = None


def _store(key: str, data: dict, ttl: int, publish: bool = False) -> None:
    payload = json.dumps({"fetched_at": int(time.time()), "data": data})
    redis_client.set(key, payload, ex=ttl)
    if publish:
        redis_client.publish(key, payload)


def _is_wanted(journey: dict) -> bool:
    if journey.get("vehicleType") != "REGIONALBUS":
        return True  # U_BAHN/S_BAHN/A_BAHN/SCHIFF are already precise
    return journey.get("line", {}).get("id") in REPLACEMENT_BUS_MODES


def _inject_tracks(response: dict) -> None:
    """Fill in each segment's geometry via the segment cache (segments come
    back without coordinates, see withoutCoords=True below). Also records
    which line/mode was observed on each segment and which stations were
    its endpoints, for the reference lines/stops layers.
    """
    keys = list(
        {
            (segment["startStopPointKey"], segment["endStopPointKey"])
            for journey in response.get("journeys", [])
            for segment in journey.get("segments", [])
            if segment.get("startStopPointKey") and segment.get("endStopPointKey")
        }
    )
    tracks = get_or_fetch_tracks(segment_cache_conn, gti, keys)

    for journey in response.get("journeys", []):
        line_name = journey.get("line", {}).get("name", "")
        mode = mode_for_journey(journey)
        for segment in journey.get("segments", []):
            key = (segment.get("startStopPointKey"), segment.get("endStopPointKey"))
            track = tracks.get(key)
            if track is not None:
                segment["track"] = {"track": track, "coordinateType": "EPSG_4326"}
            if key[0] and key[1]:
                record_line_usage(segment_cache_conn, key, line_name, mode)
            record_station_seen(segment_cache_conn, segment.get("startStationName", ""))
            record_station_seen(segment_cache_conn, segment.get("endStationName", ""))


def fetch_vehicle_map(ttl: int = VEHICLE_MAP_TTL, realtime: bool = True) -> None:
    global _last_no_realtime_response
    now = int(time.time())
    request = {
        "version": 63,
        "boundingBox": BOUNDING_BOX,
        "periodBegin": now - POST_ARRIVAL_SECONDS,
        "periodEnd": now + PRE_DEPARTURE_SECONDS,
        "withoutCoords": True,
        "coordinateType": "EPSG_4326",
        "vehicleTypes": VEHICLE_TYPES,
        "realtime": realtime,
    }
    response = gti.send("getVehicleMap", request)
    response["journeys"] = [j for j in response.get("journeys", []) if _is_wanted(j)]
    _inject_tracks(response)
    variant = "realtime" if realtime else "no_realtime"
    _store(f"hvv:vehiclemap:{variant}", response, ttl, publish=True)

    if not realtime:
        _last_no_realtime_response = response
        _rebuild_positions(ttl)


def _rebuild_positions(ttl: int = VEHICLE_MAP_TTL) -> None:
    """Re-interpolate hvv:positions from the last fetched no_realtime
    response with a fresh timestamp - no API call, just math. A segment's
    start/endDateTime don't change between fetches, so re-running the same
    interpolation against an up-to-date timestamp advances each marker
    smoothly even while the underlying data itself refreshes less often.
    """
    if _last_no_realtime_response is None:
        return
    now = int(time.time())
    _store(
        "hvv:positions",
        build_positions_geojson(_last_no_realtime_response, now_ts=now),
        ttl,
        publish=True,
    )


def fetch_announcements() -> None:
    request = {
        "language": "de",
        "version": 63,
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


def fetch_once_announcements() -> None:
    """CLI: fetch announcements once, store in Redis, then exit."""
    fetch_announcements()
    print("Stored in Redis (keys: hvv:announcements, hvv:disruptions)")


def fetch_once_vehicle_map() -> None:
    """CLI: fetch the vehicle map once for both realtime variants, store in
    Redis, then exit.

    Uses a longer TTL (5 min) than the continuous loop's 10s, so there's
    actually time to inspect it manually via redis-cli.
    """
    fetch_vehicle_map(ttl=300, realtime=True)
    fetch_vehicle_map(ttl=300, realtime=False)
    print(
        "Stored in Redis (keys: hvv:vehiclemap:realtime, "
        "hvv:vehiclemap:no_realtime, hvv:positions; expires in 5 min). "
        "Each was also published to a same-named channel."
    )


def main() -> None:
    last_announcements_fetch = 0.0
    last_reference_rebuild = 0.0
    last_vehicle_map_fetch = 0.0
    pending_reference_sublines = None  # holds fetch_sublines() result across one cycle
    realtime_toggle = True
    while True:
        cycle_start = time.time()
        try:
            if cycle_start - last_announcements_fetch >= ANNOUNCEMENTS_INTERVAL:
                fetch_announcements()
                last_announcements_fetch = cycle_start
            elif pending_reference_sublines is not None:
                # Second half, one cycle after fetch_sublines() below - its
                # own API call (listStations), kept out of the same second.
                finish_reference_rebuild(
                    gti, redis_client, segment_cache_conn, pending_reference_sublines
                )
                pending_reference_sublines = None
                last_reference_rebuild = cycle_start
            elif cycle_start - last_reference_rebuild >= REFERENCE_REBUILD_INTERVAL:
                # First half: just listLines, finished next cycle above.
                pending_reference_sublines = fetch_sublines(gti)
            elif cycle_start - last_vehicle_map_fetch >= VEHICLE_MAP_FETCH_INTERVAL:
                fetch_vehicle_map(realtime=realtime_toggle)
                realtime_toggle = not realtime_toggle
                last_vehicle_map_fetch = cycle_start
            else:
                _rebuild_positions()
        except Exception as e:  # keep the loop alive on transient API/network errors
            print(f"[hvvmap-fetcher] error: {e}", flush=True)

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, VEHICLE_MAP_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
