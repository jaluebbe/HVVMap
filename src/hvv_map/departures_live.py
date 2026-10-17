"""Short-window realtime departureList over the anchor stations: delays,
vehicle models and numbers, which the daily plan snapshot (departures.py)
cannot carry. Stored under hvv:departures:live, independent of that snapshot.
"""

import time
from datetime import datetime

from hvv_map.departures import (
    ANCHOR_STATIONS_MAIN,
    ANCHOR_STATIONS_R,
    MAIN_SERVICE_TYPES,
    R_SERVICE_TYPES,
    _store,
    berlin_now,
    merge_departures,
)
from hvv_map.gti_client import GtiClient
from hvv_map.lines import WANTED_R_BAHN_LINES
from hvv_map.redis_client import get_redis_client

REDIS_LIVE_KEY = "hvv:departures:live"
LIVE_WINDOW_MINUTES = 5
LIVE_MAX_LIST = 2000

# One request per category, covering all of its anchor stations.
LIVE_CATEGORIES = [
    (ANCHOR_STATIONS_MAIN, MAIN_SERVICE_TYPES),
    (ANCHOR_STATIONS_R, R_SERVICE_TYPES),
]


def _live_request(stations: set[str], service_types: list[str], start: datetime):
    return {
        "maxList": LIVE_MAX_LIST,
        "maxTimeOffset": LIVE_WINDOW_MINUTES,
        "useRealtime": True,
        "stations": [{"type": "STATION", "name": name} for name in sorted(stations)],
        "time": {
            "date": start.strftime("%Y-%m-%d"),
            "time": start.strftime("%H:%M:%S"),
        },
        "serviceTypes": service_types,
    }


def fetch_live_departures(client: GtiClient, start: datetime) -> dict:
    """Realtime departures per category at 1 req/s: {"departures", "anchor",
    "failed_requests"} with timeOffsets relative to the anchor (= start)."""
    responses, failed = [], 0
    for index, (stations, service_types) in enumerate(LIVE_CATEGORIES):
        if index:
            time.sleep(1)  # 1 req/s rate limit across all GTI endpoints
        response = client.send(
            "departureList", _live_request(stations, service_types, start)
        )
        if response.get("returnCode") != "OK":
            failed += 1
            continue
        if service_types == R_SERVICE_TYPES:
            response["departures"] = [
                d
                for d in response.get("departures", [])
                if d.get("line", {}).get("name") in WANTED_R_BAHN_LINES
            ]
        responses.append(response)
    return {
        "departures": merge_departures(*responses),
        "anchor": {
            "date": start.strftime("%Y-%m-%d"),
            "time": start.strftime("%H:%M:%S"),
        },
        "failed_requests": failed,
    }


def run_live_snapshot(client: GtiClient, redis_client) -> dict:
    """Fetch one realtime snapshot starting now and store it in Redis."""
    start = berlin_now().replace(second=0, microsecond=0)
    data = fetch_live_departures(client, start)
    _store(redis_client, REDIS_LIVE_KEY, data)
    return data


def main() -> None:
    """CLI: one realtime snapshot into Redis."""
    data = run_live_snapshot(GtiClient(), get_redis_client())
    print(
        f"{len(data['departures'])} live departures, "
        f"{data['failed_requests']} failed requests"
    )
