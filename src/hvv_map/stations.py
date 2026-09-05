"""Fetch and structure the HVV station catalog via listStations.

Used to resolve station IDs (from announcement locations) to coordinates.
"""

import json
import time
from dataclasses import asdict, dataclass

import redis

from hvv_map.gti_client import GtiClient

REDIS_KEY = "hvv:stations"
REDIS_TTL = 7 * 24 * 60 * 60  # 7 days - station catalog changes rarely


@dataclass(frozen=True)
class StationInfo:
    id: str
    name: str
    city: str
    lon: float
    lat: float


def fetch_stations(client: GtiClient) -> list[StationInfo]:
    """Fetch the full current station catalog (all stations, all modes)."""
    request = {
        "language": "de",
        "version": 63,
        "dataReleaseID": "",  # empty = fetch everything, not just changes
        "modificationTypes": ["MAIN", "POSITION"],
        "coordinateType": "EPSG_4326",
        # False, not True: filterEquivalent merges e.g. "Lattenkamp
        # (Sporthalle)" into the canonical "Lattenkamp" entry - but
        # getAnnouncements still references the non-canonical ID, which
        # would then be missing from our lookup. Keep every ID separate
        # instead.
        "filterEquivalent": False,
    }
    response = client.send("listStations", request)
    stations = []
    for entry in response.get("stations", []):
        coordinate = entry.get("coordinate") or {}
        if "x" not in coordinate or "y" not in coordinate:
            continue  # deleted/incomplete entries have no coordinate
        stations.append(
            StationInfo(
                id=entry.get("id", ""),
                name=entry.get("name", ""),
                city=entry.get("city", ""),
                lon=coordinate["x"],
                lat=coordinate["y"],
            )
        )
    return stations


def by_id(stations: list[StationInfo]) -> dict[str, StationInfo]:
    return {station.id: station for station in stations}


def store_stations(redis_client: redis.Redis, stations: list[StationInfo]) -> None:
    payload = json.dumps(
        {"fetched_at": int(time.time()), "data": [asdict(s) for s in stations]}
    )
    redis_client.set(REDIS_KEY, payload, ex=REDIS_TTL)


def load_stations(redis_client: redis.Redis) -> list[StationInfo]:
    payload = redis_client.get(REDIS_KEY)
    if not payload:
        return []
    return [StationInfo(**entry) for entry in json.loads(payload)["data"]]


def main() -> None:
    """CLI: fetch all stations and store them in Redis."""
    from hvv_map.redis_client import get_redis_client

    client = GtiClient()
    stations = fetch_stations(client)
    store_stations(get_redis_client(), stations)
    print(f"{len(stations)} stations stored in Redis (key: {REDIS_KEY})")


if __name__ == "__main__":
    main()
