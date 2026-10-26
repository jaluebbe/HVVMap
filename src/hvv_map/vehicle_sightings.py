"""Vehicle sightings: every live departure that carries vehicles, whether or
not it matches a plan trip. GTI reports vehicles for the U-Bahn only.

hvv:vehicles:sightings  Redis stream, one JSON entry per change of a sighting
hvv:vehicles:last       hash vehicle -> its sightings in the latest snapshot
"""

import json
import re
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


def course_sightings(request: dict, response: dict) -> list[dict]:
    """One sighting per vehicle GTI lists for a whole departureCourse."""
    vehicles = [(v, vehicle_key(v)) for v in response.get("vehicles") or []]
    vehicles = [(v, key) for v, key in vehicles if key]
    if not vehicles:
        return []
    line_id = request["lineId"]
    elements = response.get("courseElements") or []
    target = (elements[-1].get("toStation") or {}) if elements else {}
    planned = datetime.fromisoformat(request["time"]).replace(tzinfo=None)
    ubahn = re.fullmatch(r"de:hvv:U\d+:", line_id) is not None
    return [
        {
            "vehicle": key,
            "number": vehicle.get("number"),
            "id": vehicle.get("id"),
            "series": ubahn_series(vehicle.get("number")) if ubahn else None,
            "with": [other for _, other in vehicles if other != key],
            "line": line_id.split(":")[2],
            "destination": target.get("name"),
            "station": request["station"]["name"],
            "station_id": None,
            "planned": planned.isoformat(timespec="minutes"),
            "delay": None,
            "platform": None,
            "service_id": request["serviceId"],
            "cancelled": False,
            "extra": False,
            "source": "departureCourse",
        }
        for vehicle, key in vehicles
    ]


def _signature(sighting: dict) -> str:
    return (
        f"{sighting['delay']}|{sighting['platform']}|{sighting['cancelled']}|"
        + ",".join(sighting["with"])
    )


def _seen_key(sighting: dict) -> str:
    parts = ("vehicle", "service_id", "station_id", "planned")
    return SEEN_PREFIX + ":".join(str(sighting[part]) for part in parts)


def record_sightings(redis_client, live: dict, now: float | None = None) -> int:
    """Record the sightings of a live snapshot, see record."""
    return record(redis_client, extract_sightings(live), now)


def record(redis_client, sightings: list[dict], now: float | None = None) -> int:
    """Append the new or changed sightings to the stream and refresh the
    per-vehicle hash; returns the number of stream entries."""
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


JOURNEY_GAP = timedelta(hours=2)  # same serviceId this much later: another run
MIDNIGHT_GAP = timedelta(minutes=30)  # GTI renumbers trips after midnight


def unit_journeys(redis_client, unit: str, limit: int = 30) -> list[dict]:
    """The unit's recent trips from the sightings stream, newest first: per
    run of one serviceId its first and last station seen (planned times),
    line, destination, partners and whether a stop was cancelled, the trip
    an extra one or late."""
    runs: list[dict] = []
    needle = json.dumps({"vehicle": unit})[1:-1]  # cheap pre-filter
    for _, fields in redis_client.xrange(STREAM_KEY):
        if needle not in fields["data"]:
            continue
        s = json.loads(fields["data"])
        if s["vehicle"] != unit or not s.get("planned"):
            continue
        planned = datetime.fromisoformat(s["planned"])
        run = next(
            (
                r
                for r in reversed(runs)
                if r["service_id"] == str(s["service_id"])
                and abs(planned - r["_last"]) <= JOURNEY_GAP
            ),
            None,
        )
        if run is None:
            run = {
                "service_id": str(s["service_id"]),
                "line": s["line"],
                "stops": {},
                "with": set(),
                "cancelled": False,
                "extra": False,
                "max_delay": None,
                "_last": planned,
            }
            runs.append(run)
        run["_last"] = max(run["_last"], planned)
        run["destination"] = s["destination"]
        run["stops"][s["station"]] = s["planned"]
        run["with"].update(s.get("with") or [])
        run["cancelled"] |= bool(s.get("cancelled"))
        run["extra"] |= bool(s.get("extra"))
        if s.get("delay") is not None:
            run["max_delay"] = max(run["max_delay"] or 0, s["delay"])
    journeys = []
    for run in runs:
        stops = sorted(run["stops"].items(), key=lambda item: item[1])
        journeys.append(
            {
                "service_id": run["service_id"],
                "line": run["line"],
                "destination": run["destination"],
                "first": {"station": stops[0][0], "planned": stops[0][1]},
                "last": {"station": stops[-1][0], "planned": stops[-1][1]},
                "with": sorted(run["with"], key=lambda u: (len(u), u)),
                "cancelled": run["cancelled"],
                "extra": run["extra"],
                "max_delay": run["max_delay"],
            }
        )
    journeys.sort(key=lambda j: j["first"]["planned"])
    return _join_midnight(journeys)[::-1][:limit]


def _join_midnight(journeys: list[dict]) -> list[dict]:
    """One trip renumbered by GTI at midnight is one journey: same line,
    destination and partners, the second seen shortly after the first."""
    joined: list[dict] = []
    for journey in journeys:
        previous = joined[-1] if joined else None
        if previous and _continues(previous, journey):
            pair = (previous["max_delay"], journey["max_delay"])
            delays = [d for d in pair if d is not None]
            joined[-1] = {
                **previous,
                "service_id": f"{previous['service_id']}+{journey['service_id']}",
                "last": journey["last"],
                "cancelled": previous["cancelled"] or journey["cancelled"],
                "extra": previous["extra"] or journey["extra"],
                "max_delay": max(delays) if delays else None,
            }
        else:
            joined.append(journey)
    return joined


def _continues(earlier: dict, later: dict) -> bool:
    began = datetime.fromisoformat(earlier["first"]["planned"])
    end = datetime.fromisoformat(earlier["last"]["planned"])
    start = datetime.fromisoformat(later["first"]["planned"])
    return (
        earlier["line"] == later["line"]
        and earlier["destination"] == later["destination"]
        and earlier["with"] == later["with"]
        and began.date() < start.date()  # midnight in between
        and timedelta(0) <= start - end <= MIDNIGHT_GAP
    )
