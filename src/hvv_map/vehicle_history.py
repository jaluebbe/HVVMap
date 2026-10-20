"""CLI: vehicle sightings from the Redis stream written by vehicle_sightings.

Usage: hvvmap-vehicle [NUMBER ...] [--station NAME] [--line NAME]
                      [--since HH:MM] [--follow]
"""

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from hvv_map.trips import berlin_now
from hvv_map.redis_client import get_redis_client
from hvv_map.vehicle_sightings import STREAM_KEY

BERLIN_TZ = ZoneInfo("Europe/Berlin")
FOLLOW_BLOCK_MS = 5000


def parse_since(value: str, now: datetime | None = None) -> int:
    """Stream id (ms) of today's HH:MM, Berlin time."""
    hour, minute = map(int, value.split(":"))
    now = now or berlin_now()
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int(start.replace(tzinfo=BERLIN_TZ).timestamp() * 1000)


def matches(
    sighting: dict, vehicles: set[str], station: str | None, line: str | None
) -> bool:
    if vehicles and sighting["vehicle"] not in vehicles:
        return False
    if station and station.lower() not in (sighting["station"] or "").lower():
        return False
    return not line or (sighting["line"] or "").lower() == line.lower()


def format_sighting(sighting: dict) -> str:
    seen = datetime.fromtimestamp(sighting["seen_at"], BERLIN_TZ).strftime("%H:%M:%S")
    return (
        f"{seen} {sighting['vehicle']:>4} {sighting['source'][:10]:10} "
        f"{sighting['line'] or '-':3} {(sighting['station'] or '-')[:22]:22} "
        f"{sighting['planned'][11:]} -> {(sighting['destination'] or '')[:18]:18} "
        f"V {sighting['delay']} {sighting['platform'] or ''} "
        f"mit {','.join(sighting['with'])} ID {sighting['service_id']}"
    )


def _sighting(fields: dict) -> dict:
    return json.loads(fields["data"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("vehicles", nargs="*", help="unit numbers, default all")
    parser.add_argument("--station", help="part of the station name")
    parser.add_argument("--line", help="line name, e.g. U3")
    parser.add_argument("--since", help="today's HH:MM, Berlin time")
    parser.add_argument("--follow", action="store_true", help="wait for new sightings")
    args = parser.parse_args()

    client = get_redis_client()
    wanted = set(args.vehicles)
    start = str(parse_since(args.since)) if args.since else "-"
    last_id = "0-0"
    for entry_id, fields in client.xrange(STREAM_KEY, min=start):
        last_id = entry_id
        sighting = _sighting(fields)
        if matches(sighting, wanted, args.station, args.line):
            print(format_sighting(sighting), flush=True)
    while args.follow:
        for _, entries in client.xread({STREAM_KEY: last_id}, block=FOLLOW_BLOCK_MS):
            for entry_id, fields in entries:
                last_id = entry_id
                sighting = _sighting(fields)
                if matches(sighting, wanted, args.station, args.line):
                    print(format_sighting(sighting), flush=True)


if __name__ == "__main__":
    main()
