"""Fetch and structure the HVV line catalog via listLines.

Basis for deciding: icon lineKeys, which vehicleTypes to request from
getAnnouncements/getVehicleMap, and (later) per-line validity windows.
"""

import json
import re
import time
from dataclasses import dataclass

import redis

from hvv_map.gti_client import GtiClient

# U/S/AKN by name prefix (matches replacement buses too, e.g. "U1-ERSATZ",
# "S3-SEV", via the trailing ".*"). Ferries by carrier instead of name, since
# ferry line numbers alone aren't distinctive.
LINE_NAME_PATTERN = re.compile(r"^(?:[USA][0-9]{1,2}|RB(?:60|61|71|81)).*$")
FERRY_CARRIER = "HADAG"  # the actual Hamburg harbour ferry operator

# For getAnnouncements' "names" filter (accepts line names OR carrier names).
# Base line names only, no SEV/replacement-bus variants: announcements
# reference the affected original line (e.g. "A2"), not the replacement
# service's own name (e.g. "A2-SEV").
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
    "RB81",
    "RB71",
    "RB61",
    "RB60",
]
ANNOUNCEMENT_FILTER_NAMES = BASE_LINE_NAMES + [FERRY_CARRIER]

# R_BAHN covers every DB regional train passing through the bounding box, not
# just ours - filtered client-side by name wherever R_BAHN is requested.
WANTED_R_BAHN_LINES = {"RB81", "RB71", "RB61", "RB60"}

# Which original mode a REGIONALBUS-type line stands in for, derived from
# its name's U/S/A prefix - same convention as LINE_NAME_PATTERN.
_REPLACEMENT_BUS_MODE_BY_PREFIX = {"U": "U", "S": "S", "A": "AKN"}
_REPLACEMENT_BUS_NAME_PREFIX = re.compile(r"^([USA])[0-9]{1,2}")


def replacement_bus_mode(name: str) -> str:
    """Mode for a U/S/A- or RB-prefixed replacement-bus name, e.g.
    "S1-SEV" -> "S", "RB61-SEV" -> "R". "" if name matches neither."""
    match = _REPLACEMENT_BUS_NAME_PREFIX.match(name)
    if match:
        return _REPLACEMENT_BUS_MODE_BY_PREFIX[match.group(1)]
    if any(name.startswith(rb) for rb in WANTED_R_BAHN_LINES):
        return "R"
    return ""

# Official HVV line colors, by name - used for the reference lines layer.
# Replacement buses share one red across the board.
LINE_COLORS = {
    "U1": "006ab3", "U2": "e2001a", "U3": "ffdd00", "U4": "0098a1",
    "S1": "1a962b", "S2": "b51143", "S3": "622181", "S5": "0089bb", "S7": "cc7720",
    "A1": "F29400", "A2": "F29400", "A3": "F29400",
    "61": "009DD1", "62": "009DD1", "64": "009DD1", "65": "009DD1",
    "68": "009DD1", "72": "009DD1", "73": "009DD1", "75": "009DD1",
    "U1-DIREKT": "E2001A", "U1-ERSATZ": "E2001A",
    "A1-SEV": "E2001A", "A2-SEV": "E2001A", "A3-Bus": "E2001A",
    "S1-SEV": "E2001A", "S2-SEV": "E2001A", "S3-SEV": "E2001A", "S5-SEV": "E2001A", "S7-SEV": "E2001A",
    "RB60-SEV": "E2001A", "RB61-SEV": "E2001A", "RB71-SEV": "E2001A", "RB81-SEV": "E2001A",
    "RB81": "000000",
    "RB71": "000000",
    "RB61": "000000",
    "RB60": "000000",
}  # fmt: skip


@dataclass(frozen=True)
class LineInfo:
    id: str  # lineKey for the icon service, e.g. "HHA-U:U1_HHA-U"
    name: str  # e.g. "U1", "U1-ERSATZ"
    carrier_short: str
    simple_type: str  # e.g. "U_BAHN", "S_BAHN", "BUS", "SCHIFF"


def fetch_lines(client: GtiClient) -> list[LineInfo]:
    """Fetch the full current line catalog (all modes, all carriers)."""
    request = {
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


# Persisted sublines cache, one Redis key, rebuilt from scratch on every
# update (see update_sublines_cache). The (line_name, start, end) -> variants
# index is derived from this on read, not stored separately.

REDIS_SUBLINES_KEY = "hvv:sublines"
REDIS_SUBLINES_TTL = 7 * 24 * 60 * 60  # line topology changes rarely


def _sublines_by_line_id(line_entries: list[dict]) -> dict:
    """Build the persisted {line_id: {...}} dict from raw listLines entries,
    keeping only lines of interest.

    Each station carries both id and name, straight from listLines' own
    StationLight - hvv:stations is the primary name source elsewhere, but
    some stations (e.g. synthetic/combined destinations) never appear
    there."""
    result = {}
    for entry in line_entries:
        line_id = entry.get("id", "")
        if not line_id:
            continue
        service_type = entry.get("type") or {}
        line = LineInfo(
            id=line_id,
            name=entry.get("name", ""),
            carrier_short=entry.get("carrierNameShort", ""),
            simple_type=service_type.get("simpleType", ""),
        )
        if not is_line_of_interest(line):
            continue
        sublines = [
            {
                "vehicle_type": sub.get("vehicleType", ""),
                "stations": [
                    {"id": s.get("id", ""), "name": s.get("name", "")}
                    for s in sub.get("stationSequence", [])
                ],
            }
            for sub in entry.get("sublines", [])
        ]
        result[line_id] = {"line_name": line.name, "sublines": sublines}
    return result


def load_sublines_cache(redis_client: redis.Redis) -> tuple[str | None, dict]:
    payload = redis_client.get(REDIS_SUBLINES_KEY)
    if not payload:
        return None, {}
    parsed = json.loads(payload)
    return parsed.get("data_release_id"), parsed.get("lines", {})


def store_sublines_cache(
    redis_client: redis.Redis, data_release_id: str, lines: dict
) -> None:
    payload = json.dumps(
        {
            "fetched_at": int(time.time()),
            "data_release_id": data_release_id,
            "lines": lines,
        }
    )
    redis_client.set(REDIS_SUBLINES_KEY, payload, ex=REDIS_SUBLINES_TTL)


def update_sublines_cache(client: GtiClient, redis_client: redis.Redis) -> dict:
    """Full listLines fetch, replacing the persisted sublines cache in
    Redis. Returns the {line_id: {...}} dict."""
    request = {"dataReleaseID": "", "modificationTypes": ["MAIN"], "withSublines": True}
    response = client.send("listLines", request)
    merged = _sublines_by_line_id(response.get("lines", []))
    store_sublines_cache(redis_client, response.get("dataReleaseID", ""), merged)
    return merged


def sublines_from_cache(
    client: GtiClient, redis_client: redis.Redis
) -> list[SublineInfo]:
    """Reconstruct list[SublineInfo] from the persisted hvv:sublines cache -
    no API call needed once it's populated (falls back to populating it if
    empty). subline_number is left blank - unstable, never needed here."""
    _, lines = load_sublines_cache(redis_client)
    if not lines:
        lines = update_sublines_cache(client, redis_client)
    sublines = []
    for line_id, entry in lines.items():
        line_name = entry["line_name"]
        for sub in entry["sublines"]:
            sublines.append(
                SublineInfo(
                    line_name=line_name,
                    line_id=line_id,
                    subline_number="",
                    vehicle_type=sub["vehicle_type"],
                    station_ids=tuple(s["id"] for s in sub["stations"]),
                )
            )
    return sublines


def index_by_start_end(lines: dict) -> dict:
    """Nested line_name -> start_id -> end_id -> [variant] index, built from
    the merged sublines cache. Rebuilt from scratch on read, not persisted."""
    index: dict = {}
    for entry in lines.values():
        line_name = entry["line_name"]
        for sub in entry["sublines"]:
            stops = sub["stations"]
            if len(stops) < 2:
                continue
            by_start = index.setdefault(line_name, {})
            by_end = by_start.setdefault(stops[0]["id"], {})
            variants = by_end.setdefault(stops[-1]["id"], [])
            variants.append({"vehicle_type": sub["vehicle_type"], "stations": stops})
    return index


def main() -> None:
    """CLI: fetch lines of interest and print them - a lookup tool, not
    read by the running system, so nothing gets stored in Redis. Narrow the
    output with a name substring, e.g. `hvvmap-lines A1`."""
    import sys

    client = GtiClient()
    lines = fetch_lines(client)
    selected = lines_of_interest(lines)
    print(f"{len(lines)} lines total, {len(selected)} of interest")

    needle = sys.argv[1].upper() if len(sys.argv) > 1 else None
    for line in selected:
        if needle and needle not in line.name.upper():
            continue
        print(f"{line.name!r} id={line.id!r}")
        print(f"    type={line.simple_type!r} carrier={line.carrier_short!r}")


def update_sublines_cache_main() -> None:
    """CLI: update the persisted hvv:sublines cache in Redis. Same as the
    fetcher's own periodic update - see fetcher.py."""
    from hvv_map.redis_client import get_redis_client

    client = GtiClient()
    redis_client = get_redis_client()
    merged = update_sublines_cache(client, redis_client)
    print(f"{len(merged)} lines of interest cached ({REDIS_SUBLINES_KEY})")


if __name__ == "__main__":
    main()
