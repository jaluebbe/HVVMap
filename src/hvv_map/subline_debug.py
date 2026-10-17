"""CLI: why is a line drawn straight? Lists the sublines of a line name from
hvv:departures:by_service, the track status of every hop in the shapes
database and the line's departureCourse attempts (course_log)."""

import json
import sys

from hvv_map.gti_shapes import (
    REDIS_BY_SERVICE_KEY,
    apply_derived_sublines,
    open_db,
    subline_hops,
)
from hvv_map.redis_client import get_redis_client


def hop_status(segment: list | None) -> str:
    if segment is None:
        return "fehlt"
    return f"{len(segment)} Punkte" + (" (Luftlinie)" if len(segment) <= 2 else "")


def describe_subline(subline: list[dict], conn) -> list[str]:
    names = {s["id"]: s.get("name") or s["id"] for s in subline}
    ids = tuple(s["id"] for s in subline)
    position = {station_id: i for i, station_id in enumerate(ids)}
    lines = []
    for a, b, segment in subline_hops(conn, ids):
        skipped = position[b] - position[a] - 1
        note = f" [überspringt {skipped}]" if skipped > 0 else ""
        lines.append(f"  {names[a]} -> {names[b]}{note}: {hop_status(segment)}")
    return lines


def main() -> None:
    """Usage: hvvmap-subline-check <line name, e.g. S7-SEV>"""
    if len(sys.argv) != 2:
        raise SystemExit(main.__doc__)
    name = sys.argv[1]
    raw = get_redis_client().get(REDIS_BY_SERVICE_KEY)
    by_service = json.loads(raw)["data"] if raw else {}
    conn = open_db()
    apply_derived_sublines(conn, by_service)
    entries = [e for e in by_service.values() if e["line"].get("name") == name]
    print(f"{len(entries)} serviceIds of {name} in by_service")
    sublines = {}
    unresolved = 0
    for service_id, entry in by_service.items():
        if entry["line"].get("name") != name or entry.get("subline"):
            continue
        unresolved += 1
        stops = " > ".join(
            (s.get("station") or {}).get("combinedName", "?") for s in entry["stops"]
        )
        print(f"unresolved {service_id} -> {entry.get('destination')}: {stops}")
    for entry in entries:
        if not entry.get("subline"):
            continue
        key = tuple(s["id"] for s in entry["subline"])
        sublines.setdefault(key, entry["subline"])
    print(f"{unresolved} without a resolved subline")

    for number, subline in enumerate(sublines.values(), start=1):
        print(f"\nsubline {number}: {len(subline)} stations")
        print("\n".join(describe_subline(subline, conn)))

    print(f"\ncourse_log of {name}:")
    for row in conn.execute(
        "SELECT called_at, station, dep_time, result, segments FROM course_log "
        "WHERE line_name = ? ORDER BY id DESC LIMIT 10",
        (name,),
    ):
        print(f"  {row}")
