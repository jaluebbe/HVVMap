"""Compare the planned U-Bahn trips (hvv:departures:by_service) with the
realtime departures (hvv:departures:live): which live entry belongs to which
planned trip, and which planned trips the live window does not show.

Pure functions over the cached payloads, so the api image needs no GTI client.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from hvv_map.departures_tree import (
    _is_ubahn,
    _natural_key,
    _station_name,
    parse_anchor,
    refined_model,
    vehicles_text,
)

LIVE_WINDOW_MINUTES = 5  # keep in sync with departures_live.LIVE_WINDOW_MINUTES
MATCH_TOLERANCE_MINUTES = 1  # planned times may differ by a rounding minute
GAP_THRESHOLD_MINUTES = 120  # same as departures.GAP_THRESHOLD_MINUTES

STATUS_CONFIRMED = "bestätigt"
STATUS_NOT_SEEN = "nicht gesehen"
STATUS_CANCELLED = "ausgefallen"


_COUNTER_KEY = {
    STATUS_CONFIRMED: "confirmed",
    STATUS_NOT_SEEN: "not_seen",
    STATUS_CANCELLED: "cancelled",
}


def _split_occurrences(stops: list[dict]) -> list[list[dict]]:
    """A serviceId's stops split where a gap marks its next operating day."""
    runs: list[list[dict]] = []
    for stop in stops:
        if runs and stop["timeOffset"] - runs[-1][-1]["timeOffset"] <= (
            GAP_THRESHOLD_MINUTES
        ):
            runs[-1].append(stop)
        else:
            runs.append([stop])
    return runs


def _plan_trips(by_service: dict, plan_anchor: datetime) -> list[dict]:
    """One entry per U-Bahn trip occurrence: its anchor stops with absolute
    planned times."""
    trips = []
    for service_id, entry in by_service.items():
        line = entry.get("line") or {}
        stops = [s for s in entry.get("stops", []) if s.get("timeOffset") is not None]
        if not _is_ubahn(line) or not stops:
            continue
        for number, run in enumerate(_split_occurrences(stops), start=1):
            trips.append(
                {
                    "id": service_id if number == 1 else f"{service_id}#{number}",
                    "service_id": service_id,
                    "line": line.get("name", ""),
                    "line_obj": line,
                    "direction_id": entry.get("directionId"),
                    "destination": entry.get("destination"),
                    "subline": entry.get("subline"),
                    "stops": [
                        {
                            "station_id": (s.get("station") or {}).get("id"),
                            "station": _station_name(s.get("station") or {}),
                            "direction": s.get("direction"),
                            "platform": s.get("realtimePlatform") or s.get("platform"),
                            "planned": plan_anchor + timedelta(minutes=s["timeOffset"]),
                            "match": None,
                        }
                        for s in run
                    ],
                }
            )
    return trips


def course_key(trip: dict) -> str:
    """Cache key of a trip occurrence's departureCourse."""
    return f"{trip['service_id']}@{trip['stops'][0]['planned']:%Y-%m-%dT%H:%M}"


def _base_stations(trip: dict, course: dict | None) -> list[dict]:
    """Station sequence with the times known for it: from the trip's
    departureCourse if cached, else from its subline (no times)."""
    if course and course.get("stops"):
        stations = []
        for stop in course["stops"]:
            clock = stop.get("dep") or stop.get("arr")
            stations.append(
                {
                    "id": stop["id"],
                    "name": stop["name"],
                    "time": datetime.fromisoformat(clock) if clock else None,
                    "delay": stop["dep_delay"]
                    if stop.get("dep") and stop["dep_delay"] is not None
                    else stop.get("arr_delay"),
                }
            )
        return stations
    return [
        {"id": s["id"], "name": s["name"], "time": None, "delay": None}
        for s in trip.get("subline") or []
    ]


def _anchor_positions(anchors: list[dict], stations: list[dict]) -> list[int]:
    """Index of every anchor stop within the station sequence, in order;
    empty if one of them cannot be placed."""
    positions: list[int] = []
    start = 0
    for stop in anchors:
        found = next(
            (
                i
                for i in range(start, len(stations))
                if stations[i]["id"] == stop["station_id"]
            ),
            None,
        )
        if found is None:
            return []
        positions.append(found)
        start = found + 1
    return positions


def _route_rows(
    trip: dict, live_anchor: datetime, window_end: datetime, course: dict | None
) -> list[dict]:
    """Full station sequence of a trip. Anchor stops keep their planned time
    and live match; other stations take the departureCourse time if cached
    (source "course"), else a time interpolated linearly between the
    neighbouring anchors (source "interpolated"), else none. Falls back to the
    anchor stops alone if they cannot be placed."""
    anchors = trip["stops"]
    stations = _base_stations(trip, course)
    positions = _anchor_positions(anchors, stations)
    if not positions and course and course.get("stops"):
        stations = _base_stations(trip, None)
        positions = _anchor_positions(anchors, stations)

    def anchor_row(stop: dict, delay=None) -> dict:
        return {
            "station": stop["station"],
            "planned": stop["planned"].strftime("%H:%M"),
            "at": stop["planned"].isoformat(timespec="minutes"),
            "source": "plan",
            "course_delay": delay,
            "interpolated": False,
            "anchor": True,
            "expected": live_anchor <= stop["planned"] <= window_end,
            "match": _public(stop["match"]) if stop["match"] else None,
        }

    if not positions:
        return [anchor_row(stop) for stop in anchors]

    rows = []
    for i, station in enumerate(stations):
        if i in positions:
            rows.append(anchor_row(anchors[positions.index(i)], station["delay"]))
            continue
        at, source = None, None
        if station["time"] is not None:
            at, source = station["time"], "course"
        else:
            for k in range(len(positions) - 1):
                lo, hi = positions[k], positions[k + 1]
                if lo < i < hi:
                    t0, t1 = anchors[k]["planned"], anchors[k + 1]["planned"]
                    moved = (t1 - t0) * (i - lo) / (hi - lo)
                    at = (t0 + moved + timedelta(seconds=30)).replace(second=0)
                    source = "interpolated"
                    break
        rows.append(
            {
                "station": station["name"],
                "planned": at.strftime("%H:%M") if at else None,
                "at": at.isoformat(timespec="minutes") if at else None,
                "source": source,
                "course_delay": station["delay"],
                "interpolated": source == "interpolated",
                "anchor": False,
                "expected": False,
                "match": None,
            }
        )
    return rows


def _index_plan(trips: list[dict]) -> dict:
    """(line, station id, planned minute, direction id) -> [(trip, stop)]."""
    index = defaultdict(list)
    for trip in trips:
        for stop in trip["stops"]:
            key = (
                trip["line"],
                stop["station_id"],
                stop["planned"],
                trip["direction_id"],
            )
            index[key].append((trip, stop))
    return index


def _find_candidates(index: dict, key: tuple) -> tuple[list, str]:
    """Candidates for a live departure's key: exact minute first, then the
    neighbouring minutes. Returns (candidates, quality)."""
    line, station_id, planned, direction_id = key
    if index.get(key):
        return index[key], "exakt"
    for delta in range(1, MATCH_TOLERANCE_MINUTES + 1):
        for sign in (-1, 1):
            moved = (
                line,
                station_id,
                planned + timedelta(minutes=sign * delta),
                direction_id,
            )
            if index.get(moved):
                return index[moved], f"±{delta} min"
    return [], ""


def _live_entry(dep: dict, planned: datetime) -> dict:
    line = dep.get("line") or {}
    vehicles = dep.get("vehicles")
    return {
        "service_id": dep.get("serviceId"),
        "line": line.get("name", ""),
        "direction_id": dep.get("directionId"),
        "station": _station_name(dep.get("station") or {}),
        "planned": planned.strftime("%H:%M"),
        "destination": line.get("direction"),
        "platform": dep.get("realtimePlatform") or dep.get("platform"),
        "delay": dep.get("delay"),
        "cancelled": bool(dep.get("cancelled")),
        "extra": bool(dep.get("extra")),
        "vehicles": vehicles_text(vehicles, True),
        "model": refined_model(line, vehicles),
        "_vehicles": vehicles or [],
    }


def _public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def build_trip_table(
    by_service: dict,
    plan_anchor: datetime | None,
    live: dict,
    courses: dict | None = None,
) -> dict:
    """Plan trips of the U-Bahn matched against the live departures:
    {"summary", "trips", "live_only"}. A live departure matches the plan
    stop with the same line, station, direction id and planned minute; a
    plan trip is "nicht gesehen" if one of its stops lay inside the live
    window but no live departure matched any of its stops."""
    live_anchor = parse_anchor(live.get("anchor"))
    if plan_anchor is None or live_anchor is None:
        return {"summary": {}, "trips": [], "live_only": []}

    trips = _plan_trips(by_service, plan_anchor)
    index = _index_plan(trips)
    window_end = live_anchor + timedelta(minutes=LIVE_WINDOW_MINUTES)

    live_only, quality_counts, live_total = [], defaultdict(int), 0
    per_line: dict[str, dict] = defaultdict(
        lambda: {
            "live": 0,
            "matched": 0,
            "trips": 0,
            "confirmed": 0,
            "not_seen": 0,
            "cancelled": 0,
        }
    )
    for dep in live.get("departures", []):
        line = dep.get("line") or {}
        if not _is_ubahn(line) or dep.get("timeOffset") is None:
            continue
        live_total += 1
        planned = live_anchor + timedelta(minutes=dep["timeOffset"])
        entry = _live_entry(dep, planned)
        per_line[entry["line"]]["live"] += 1
        key = (
            entry["line"],
            (dep.get("station") or {}).get("id"),
            planned,
            dep.get("directionId"),
        )
        candidates, quality = _find_candidates(index, key)
        free = [(t, s) for t, s in candidates if s["match"] is None]
        if len(free) > 1 and entry["platform"]:
            same_platform = [
                (t, s) for t, s in free if s["platform"] == entry["platform"]
            ]
            free = same_platform or free
        if not free:
            live_only.append(
                {
                    **_public(entry),
                    "reason": "Zusatzfahrt (extra)"
                    if entry["extra"]
                    else "ohne Plan-Treffer"
                    if not candidates
                    else "Plan-Halt schon vergeben",
                }
            )
            continue
        trip, stop = free[0]
        if len(free) > 1:
            quality = f"{quality}, mehrdeutig"
        quality_counts[quality] += 1
        per_line[entry["line"]]["matched"] += 1
        stop["match"] = {
            **_public(entry),
            "quality": quality,
            "destination_changed": entry["destination"] != stop["direction"],
            "_vehicles": entry["_vehicles"],
        }

    rows = []
    for trip in trips:
        matched = [s for s in trip["stops"] if s["match"]]
        expected = [
            s for s in trip["stops"] if live_anchor <= s["planned"] <= window_end
        ]
        if not matched and not expected:
            continue
        cancelled = bool(matched) and all(s["match"]["cancelled"] for s in matched)
        if cancelled:
            status = STATUS_CANCELLED
        else:
            status = STATUS_CONFIRMED if matched else STATUS_NOT_SEEN
        vehicles = [v for s in matched for v in s["match"]["_vehicles"]]
        unique = list({(v.get("id"), v.get("number")): v for v in vehicles}.values())
        delays = [
            s["match"]["delay"] for s in matched if s["match"]["delay"] is not None
        ]
        relevant = matched or expected
        rows.append(
            {
                "id": trip["id"],
                "service_id": trip["service_id"],
                "line": trip["line"],
                "direction_id": trip["direction_id"],
                "destination": trip["destination"],
                "status": status,
                "start_time": trip["stops"][0]["planned"].strftime("%H:%M"),
                "start_station": trip["stops"][0]["station"],
                "window_time": relevant[0]["planned"].strftime("%H:%M"),
                "window_station": relevant[0]["station"],
                "live_ids": sorted({str(s["match"]["service_id"]) for s in matched}),
                "destination_changed": any(
                    s["match"]["destination_changed"] for s in matched
                ),
                "max_delay": max(delays) if delays else None,
                "vehicles": vehicles_text(unique, True),
                "model": refined_model(trip["line_obj"], unique),
                "route_start": None,
                "route_end": None,
                "stops": _route_rows(
                    trip, live_anchor, window_end, (courses or {}).get(course_key(trip))
                ),
                "_sort": trip["stops"][0]["planned"],
            }
        )
        per_line[trip["line"]]["trips"] += 1
        per_line[trip["line"]][_COUNTER_KEY[status]] += 1

    for row in rows:
        times = [s["at"] for s in row["stops"] if s["at"]]
        row["route_start"], row["route_end"] = min(times), max(times)
    rows.sort(key=lambda r: (r["_sort"], _natural_key(r["line"])))
    for row in rows:
        del row["_sort"]
    live_only.sort(key=lambda e: (e["planned"], _natural_key(e["line"])))

    matched_total = sum(quality_counts.values())
    return {
        "summary": {
            "live_total": live_total,
            "live_matched": matched_total,
            "live_unmatched": len(live_only),
            "quality": dict(quality_counts),
            "trips": len(rows),
            "confirmed": sum(r["status"] == STATUS_CONFIRMED for r in rows),
            "not_seen": sum(r["status"] == STATUS_NOT_SEEN for r in rows),
            "cancelled": sum(r["status"] == STATUS_CANCELLED for r in rows),
            "extra": sum(e["extra"] for e in live_only),
            "by_line": {
                name: per_line[name] for name in sorted(per_line, key=_natural_key)
            },
            "live_anchor": live_anchor.strftime("%Y-%m-%d %H:%M"),
            "plan_anchor": plan_anchor.strftime("%Y-%m-%d %H:%M"),
        },
        "trips": rows,
        "live_only": live_only,
    }
