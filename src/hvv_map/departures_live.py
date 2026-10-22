"""Short-window realtime departureList over the anchor stations: delays,
vehicle models and numbers, which the daily plan snapshot (departures.py)
cannot carry. Stored under hvv:departures:live, independent of that snapshot.
"""

import json
import time
from datetime import datetime

import redis
import requests

from hvv_map.departures import (
    ANCHOR_STATIONS_MAIN,
    ANCHOR_STATIONS_R,
    MAIN_SERVICE_TYPES,
    R_SERVICE_TYPES,
    REDIS_ANNOUNCEMENT_STATIONS_KEY,
    SEV_SERVICE_TYPES,
    _is_wanted_departure,
    _store,
    berlin_now,
    merge_departures,
)
from hvv_map.gti_client import GtiClient
from hvv_map.lines import WANTED_R_BAHN_LINES
from hvv_map.redis_client import get_redis_client
from hvv_map.vehicle_sightings import record_sightings

REDIS_LIVE_KEY = "hvv:departures:live"
LIVE_WINDOW_MINUTES = 5
LIVE_WINDOW_LONG_MINUTES = 20  # regional trains and replacement buses stop rarely
LIVE_MAX_LIST = 2000

# One request per category, covering all of its anchor stations.
LIVE_CATEGORIES = [
    (ANCHOR_STATIONS_MAIN, MAIN_SERVICE_TYPES),
    (ANCHOR_STATIONS_R, R_SERVICE_TYPES),
]


def _live_request(
    stations: set[str],
    service_types: list[str],
    start: datetime,
    minutes: int = LIVE_WINDOW_MINUTES,
):
    return {
        "maxList": LIVE_MAX_LIST,
        "maxTimeOffset": minutes,
        "useRealtime": True,
        "allowCancellations": True,  # else cancelled departures are left out
        "stations": [{"type": "STATION", "name": name} for name in sorted(stations)],
        "time": {
            "date": start.strftime("%Y-%m-%d"),
            "time": start.strftime("%H:%M:%S"),
        },
        "serviceTypes": service_types,
    }


LIVE_CHUNK_STATIONS = 20  # stations per request: big requests time out (504)
LIVE_RETRY_PAUSE = 2  # seconds before the one retry of a failed request


def _chunks(stations: set[str]) -> list[set[str]]:
    names = sorted(stations)
    return [
        set(names[i : i + LIVE_CHUNK_STATIONS])
        for i in range(0, len(names), LIVE_CHUNK_STATIONS)
    ]


def _send(client: GtiClient, request: dict) -> tuple[dict | None, str]:
    """(response, error): one retry after an HTTP error or a non-OK answer."""
    error = ""
    for attempt in range(2):
        if attempt:
            time.sleep(LIVE_RETRY_PAUSE)
        try:
            response = client.send("departureList", request)
        except requests.RequestException as exc:
            error = str(exc)
            continue
        if response.get("returnCode") == "OK":
            return response, ""
        error = str(response.get("returnCode"))
    return None, error


def fetch_live_departures(
    client: GtiClient, start: datetime, sev_stations: set[str] | None = None
) -> dict:
    """Realtime departures in requests of at most LIVE_CHUNK_STATIONS stations
    per category, at 1 req/s: {"departures", "anchor", "failed_requests",
    "errors", "complete"} with timeOffsets relative to the anchor (= start).
    sev_stations (stations of active announcements) adds a replacement-bus
    category. "complete" is False if a request of the main category failed:
    without it, U and S trips would look cancelled."""
    categories = list(LIVE_CATEGORIES)
    if sev_stations:
        categories.append((sev_stations, SEV_SERVICE_TYPES))
    responses, errors, complete, first = [], [], True, True
    for stations, service_types in categories:
        minutes = (
            LIVE_WINDOW_MINUTES
            if service_types == MAIN_SERVICE_TYPES
            else LIVE_WINDOW_LONG_MINUTES
        )
        chunks = _chunks(stations)
        for number, chunk in enumerate(chunks, start=1):
            if not first:
                time.sleep(1)  # 1 req/s rate limit across all GTI endpoints
            first = False
            response, error = _send(
                client, _live_request(chunk, service_types, start, minutes)
            )
            if response is None:
                errors.append(
                    f"{'/'.join(service_types)} {number}/{len(chunks)}: {error}"
                )
                complete = complete and service_types != MAIN_SERVICE_TYPES
                continue
            if service_types == SEV_SERVICE_TYPES:
                response["departures"] = [
                    d for d in response.get("departures", []) if _is_wanted_departure(d)
                ]
            elif service_types == R_SERVICE_TYPES:
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
        "failed_requests": len(errors),
        "errors": errors,
        "complete": complete,
    }


def run_live_snapshot(client: GtiClient, redis_client) -> dict:
    """Fetch one realtime snapshot starting now; stored in Redis only if
    complete (see fetch_live_departures), else the old one stays."""
    start = berlin_now().replace(second=0, microsecond=0)
    payload = redis_client.get(REDIS_ANNOUNCEMENT_STATIONS_KEY)
    sev_stations = set(json.loads(payload)["data"]) if payload else set()
    data = fetch_live_departures(client, start, sev_stations)
    if not data.get("complete", True):
        return data
    _store(redis_client, REDIS_LIVE_KEY, data)
    try:
        record_sightings(redis_client, data)
    except redis.RedisError as error:
        print(f"[departures_live] vehicle sightings: {error}", flush=True)
    return data


def main() -> None:
    """CLI: one realtime snapshot into Redis."""
    data = run_live_snapshot(GtiClient(), get_redis_client())
    print(
        f"{len(data['departures'])} live departures, "
        f"{data['failed_requests']} failed requests"
    )
