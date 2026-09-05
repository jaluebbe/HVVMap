"""Fetch and structure the HVV line catalog via listLines.

Basis for deciding: icon lineKeys, which vehicleTypes to request from
getAnnouncements/getVehicleMap, and (later) per-line validity windows.
"""

import json
import re
import time
from dataclasses import asdict, dataclass

import redis

from hvv_map.gti_client import GtiClient
from hvv_map.redis_client import get_redis_client

# U/S/AKN by name prefix (matches replacement buses too, e.g. "U1-ERSATZ",
# "S3-SEV", via the trailing ".*"). Ferries by carrier instead of name, since
# ferry line numbers alone aren't distinctive.
LINE_NAME_PATTERN = re.compile(r"^[USA][0-9]{1,2}.*$")
FERRY_CARRIER = "HADAG"  # confirmed: the actual Hamburg harbour ferry operator

# For getAnnouncements' "names" filter (accepts line names OR carrier names).
# Base line names only, no SEV/replacement-bus variants: announcements
# reference the affected original line (e.g. "A2"), not the replacement
# service's own name (e.g. "A2-SEV") - confirmed from real sample data.
BASE_LINE_NAMES = [
    "U1",
    "U2",
    "U3",
    "U4",
    "S1",
    "S2",
    "S3",
    "S5",
    "S7",
    "A1",
    "A2",
    "A3",
]
ANNOUNCEMENT_FILTER_NAMES = BASE_LINE_NAMES + [FERRY_CARRIER]

# Confirmed replacement-bus lineKeys and which mode they stand in for (same
# convention as MODE_BY_ROUTE_TYPE in the GTFS-based system). U1-ERSATZ/
# U1-DIREKT confirmed live via getVehicleMap; the rest assumed to share the
# same REGIONALBUS category (same kind of service) but not yet individually
# confirmed live - worth spot-checking when one happens to be running.
REPLACEMENT_BUS_MODES = {
    "HHA-B:U1-ERSATZ_HHA-B": "U",
    "HHA-B:U1-DIREKT_HHA-B": "U",
    "DB-EFZ:A1-SEV_DB-EFZ_Z": "AKN",
    "DB-EFZ:A2-SEV_DB-EFZ_Z": "AKN",
    "VHH:A3-Bus_VHH": "AKN",
    "SBH:S3-SEV_SBH_SBAHNS": "S",
    "SBH:S5-SEV_SBH_SBAHNS": "S",
    "SBH:S7-SEV_SBH_SBAHNS": "S",
}

# Official HVV line colors, by name - used for the reference lines layer.
# Replacement buses share one red across the board (matches their own
# route_text_color where GTFS specifies it - see the old GTFS-based system).
LINE_COLORS = {
    "U1": "006ab3", "U2": "e2001a", "U3": "ffdd00", "U4": "0098a1",
    "S1": "1a962b", "S2": "b51143", "S3": "622181", "S5": "0089bb", "S7": "cc7720",
    "A1": "F29400", "A2": "F29400", "A3": "F29400",
    "61": "009DD1", "62": "009DD1", "64": "009DD1", "65": "009DD1",
    "68": "009DD1", "72": "009DD1", "73": "009DD1", "75": "009DD1",
    "U1-DIREKT": "E2001A", "U1-ERSATZ": "E2001A",
    "A1-SEV": "E2001A", "A2-SEV": "E2001A", "A3-Bus": "E2001A",
    "S3-SEV": "E2001A", "S5-SEV": "E2001A", "S7-SEV": "E2001A",
}  # fmt: skip

REDIS_KEY = "hvv:lines"
REDIS_TTL = 24 * 60 * 60  # seconds; catalog changes rarely, generous headroom


@dataclass(frozen=True)
class LineInfo:
    id: str  # lineKey for the icon service, e.g. "HHA-U:U1_HHA-U"
    name: str  # e.g. "U1", "U1-ERSATZ"
    carrier_short: str
    simple_type: str  # e.g. "U_BAHN", "S_BAHN", "BUS", "SCHIFF"


def fetch_lines(client: GtiClient) -> list[LineInfo]:
    """Fetch the full current line catalog (all modes, all carriers)."""
    request = {
        "language": "de",
        "version": 63,
        "dataReleaseID": "",  # empty = fetch everything, not just changes
        "modificationTypes": ["MAIN"],
    }
    response = client.send("listLines", request)
    lines = []
    for entry in response.get("lines", []):
        service_type = entry.get("type") or {}
        lines.append(
            LineInfo(
                id=entry.get("id", ""),
                name=entry.get("name", ""),
                carrier_short=entry.get("carrierNameShort", ""),
                simple_type=service_type.get("simpleType", ""),
            )
        )
    return lines


def by_name(lines: list[LineInfo]) -> dict[str, LineInfo]:
    """Index by name for quick lookup, e.g. by_name(lines)["U1"].id."""
    return {line.name: line for line in lines}


def simple_types_used(lines: list[LineInfo]) -> set[str]:
    """Distinct simpleType values present - handy for picking vehicleTypes."""
    return {line.simple_type for line in lines if line.simple_type}


def is_line_of_interest(line: LineInfo) -> bool:
    """U/S/AKN (incl. replacement buses) by name, ferries by carrier."""
    return (
        bool(LINE_NAME_PATTERN.match(line.name)) or line.carrier_short == FERRY_CARRIER
    )


def lines_of_interest(lines: list[LineInfo]) -> list[LineInfo]:
    return [line for line in lines if is_line_of_interest(line)]


@dataclass(frozen=True)
class SublineInfo:
    line_name: str  # e.g. "U1"
    line_id: str  # lineKey, e.g. "HHA-U:U1_HHA-U" - for REPLACEMENT_BUS_MODES lookup
    subline_number: str  # unstable across data versions, don't persist as a key
    vehicle_type: str  # e.g. "U_BAHN", "REGIONALBUS"
    station_ids: tuple[str, ...]  # ordered, per SublineListEntry.stationSequence


def fetch_sublines(client: GtiClient) -> list[SublineInfo]:
    """Fetch all sublines (with their ordered station sequence) for lines of
    interest - the basis for building a reference stops/lines layer without
    GTFS. StationLight only has id+name, no coordinates - resolve via
    stations.py separately."""
    request = {
        "language": "de",
        "version": 63,
        "dataReleaseID": "",
        "modificationTypes": ["MAIN"],
        "withSublines": True,
    }
    response = client.send("listLines", request)
    sublines = []
    for entry in response.get("lines", []):
        service_type = entry.get("type") or {}
        line = LineInfo(
            id=entry.get("id", ""),
            name=entry.get("name", ""),
            carrier_short=entry.get("carrierNameShort", ""),
            simple_type=service_type.get("simpleType", ""),
        )
        if not is_line_of_interest(line):
            continue
        for sub in entry.get("sublines", []):
            station_ids = tuple(s.get("id", "") for s in sub.get("stationSequence", []))
            sublines.append(
                SublineInfo(
                    line_name=line.name,
                    line_id=line.id,
                    subline_number=sub.get("sublineNumber", ""),
                    vehicle_type=sub.get("vehicleType", ""),
                    station_ids=station_ids,
                )
            )
    return sublines


def store_lines(redis_client: redis.Redis, lines: list[LineInfo]) -> None:
    payload = json.dumps(
        {"fetched_at": int(time.time()), "data": [asdict(line) for line in lines]}
    )
    redis_client.set(REDIS_KEY, payload, ex=REDIS_TTL)


def load_lines(redis_client: redis.Redis) -> list[LineInfo]:
    payload = redis_client.get(REDIS_KEY)
    if not payload:
        return []
    return [LineInfo(**entry) for entry in json.loads(payload)["data"]]


def main() -> None:
    """CLI: fetch lines of interest, store them in Redis, and print them -
    optionally narrowed further by a name substring."""
    import sys

    client = GtiClient()
    lines = fetch_lines(client)
    selected = lines_of_interest(lines)
    store_lines(get_redis_client(), selected)
    print(f"{len(lines)} lines total, {len(selected)} of interest (stored in Redis)")

    needle = sys.argv[1].upper() if len(sys.argv) > 1 else None
    for line in selected:
        if needle and needle not in line.name.upper():
            continue
        print(f"{line.name!r} id={line.id!r}")
        print(f"    type={line.simple_type!r} carrier={line.carrier_short!r}")


if __name__ == "__main__":
    main()
