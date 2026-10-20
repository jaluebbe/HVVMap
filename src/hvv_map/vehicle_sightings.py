"""Vehicle sightings: every live departure that carries vehicles, whether or
not it matches a plan trip. GTI reports vehicles for the U-Bahn only.

hvv:vehicles:sightings  Redis stream, one JSON entry per change of a sighting
hvv:vehicles:last       hash vehicle -> its sightings in the latest snapshot
"""

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta

from hvv_map.departures_tree import _is_ubahn, ubahn_series

STREAM_KEY = "hvv:vehicles:sightings"
LAST_KEY = "hvv:vehicles:last"
STREAM_MAXLEN = 50_000
SEEN_PREFIX = "hvv:vehicles:seen:"
SEEN_TTL = 3600  # an unchanged sighting is not written again within this time


def vehicle_key(vehicle: dict) -> str | None:
    """The unit number, else the GTI vehicle id."""
    if vehicle.get("number"):
        return str(vehicle["number"])
    return f"id:{vehicle['id']}" if vehicle.get("id") else None


def extract_sightings(live: dict) -> list[dict]:
    """One sighting per vehicle of every live departure that lists vehicles."""
    anchor = live.get("anchor") or {}
    try:
        start = datetime.strptime(
            f"{anchor['date']} {anchor['time']}", "%Y-%m-%d %H:%M:%S"
        )
    except (KeyError, ValueError):
        return []
    sightings = []
    for dep in live.get("departures", []):
        vehicles = [
            (vehicle, vehicle_key(vehicle)) for vehicle in dep.get("vehicles") or []
        ]
        vehicles = [(v, key) for v, key in vehicles if key]
        if not vehicles or dep.get("timeOffset") is None:
            continue
        line = dep.get("line") or {}
        station = dep.get("station") or {}
        planned = start + timedelta(minutes=dep["timeOffset"])
        for vehicle, key in vehicles:
            sightings.append(
                {
                    "vehicle": key,
                    "number": vehicle.get("number"),
                    "id": vehicle.get("id"),
                    "series": ubahn_series(vehicle.get("number"))
                    if _is_ubahn(line)
                    else None,
                    "with": [other for _, other in vehicles if other != key],
                    "line": line.get("name"),
                    "destination": line.get("direction"),
                    "station": station.get("combinedName") or station.get("name"),
                    "station_id": station.get("id"),
                    "planned": planned.isoformat(timespec="minutes"),
                    "delay": dep.get("delay"),
                    "platform": dep.get("realtimePlatform") or dep.get("platform"),
                    "service_id": dep.get("serviceId"),
                    "cancelled": bool(dep.get("cancelled")),
                    "extra": bool(dep.get("extra")),
                    "source": "departureList",
                }
            )
    return sightings


def _signature(sighting: dict) -> str:
    return f"{sighting['delay']}|{sighting['platform']}|{sighting['cancelled']}"


def _seen_key(sighting: dict) -> str:
    parts = ("vehicle", "service_id", "station_id", "planned")
    return SEEN_PREFIX + ":".join(str(sighting[part]) for part in parts)


def record_sightings(redis_client, live: dict, now: float | None = None) -> int:
    """Append the new or changed sightings of a live snapshot to the stream and
    refresh the per-vehicle hash; returns the number of stream entries."""
    sightings = extract_sightings(live)
    if not sightings:
        return 0
    seen_at = int(time.time() if now is None else now)
    pipe = redis_client.pipeline()
    for s in sightings:
        pipe.set(_seen_key(s), _signature(s), ex=SEEN_TTL, get=True)
    previous = pipe.execute()
    changed = [s for s, old in zip(sightings, previous) if old != _signature(s)]

    by_vehicle = defaultdict(list)
    for s in sightings:
        by_vehicle[s["vehicle"]].append(s)
    pipe = redis_client.pipeline()
    for s in changed:
        entry = json.dumps({**s, "seen_at": seen_at}, ensure_ascii=False)
        pipe.xadd(STREAM_KEY, {"data": entry}, maxlen=STREAM_MAXLEN, approximate=True)
    for vehicle, items in by_vehicle.items():
        items.sort(key=lambda s: s["planned"])
        value = json.dumps({"seen_at": seen_at, "sightings": items}, ensure_ascii=False)
        pipe.hset(LAST_KEY, vehicle, value)
    pipe.execute()
    return len(changed)
