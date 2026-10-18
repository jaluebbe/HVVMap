"""Compare the planned trips (hvv:departures:by_service) with the
realtime departures (hvv:departures:live): which live entry belongs to which
planned trip, and which planned trips the live window does not show.

Pure functions over the cached payloads, so the api image needs no GTI client.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from hvv_map.gtilive_geojson import mode_for_line
from hvv_map.departures_tree import (
    _is_ubahn,
    _natural_key,
    _station_name,
    parse_anchor,
    refined_model,
    vehicles_text,
)

LIVE_WINDOW_MINUTES = 5  # keep in sync with departures_live.LIVE_WINDOW_MINUTES
MATCH_TOLERANCE_MINUTES = 0  # time-only matches: exact minute
ID_MATCH_WINDOW_MINUTES = 5  # same serviceId at the station: accept this offset
GAP_THRESHOLD_MINUTES = 120  # same as departures.GAP_THRESHOLD_MINUTES

STATUS_CONFIRMED = "bestätigt"
STATUS_NOT_SEEN = "nicht gesehen"
STATUS_CANCELLED = "ausgefallen"
STATUS_BETWEEN = "zwischen Ankern"  # running, no anchor stop in the live window


def _is_trip_line(line: dict) -> bool:
    """U, S, AKN, ferry, RB/RE and their replacement buses."""
    return bool(mode_for_line(line))


def _mode_label(line: dict) -> str:
    """Map mode key of a line; replacement buses are listed as "SEV"."""
    if (line.get("type") or {}).get("shortInfo") == "Bus":
        return "SEV"
    return mode_for_line(line)


_COUNTER_KEY = {
    STATUS_CONFIRMED: "confirmed",
    STATUS_NOT_SEEN: "not_seen",
    STATUS_CANCELLED: "cancelled",
    STATUS_BETWEEN: "between",
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
    """One entry per trip occurrence: its anchor stops with absolute
    planned times."""
    trips = []
    for service_id, entry in by_service.items():
        line = entry.get("line") or {}
        stops = [s for s in entry.get("stops", []) if s.get("timeOffset") is not None]
        if not _is_trip_line(line) or not stops:
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


def _hm(iso: str | None) -> str | None:
    """HH:MM of an ISO timestamp."""
    return iso[11:16] if iso else None


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
                    "arr": _hm(stop.get("arr")),
                    "dep": _hm(stop.get("dep")),
                    "platform": stop.get("platform"),
                    "time": datetime.fromisoformat(clock) if clock else None,
                    "delay": stop["dep_delay"]
                    if stop.get("dep") and stop["dep_delay"] is not None
                    else stop.get("arr_delay"),
                }
            )
        return stations
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "arr": None,
            "dep": None,
            "platform": None,
            "time": None,
            "delay": None,
        }
        for s in trip.get("subline") or []
    ]


def _live_or_course(match: dict | None, course_delay: int | None) -> int | None:
    """Delay of a stop: the live value if matched, else the course's."""
    if match and match.get("delay") is not None:
        return match["delay"]
    return course_delay


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

    def anchor_row(stop: dict, station: dict | None = None) -> dict:
        planned = stop["planned"].strftime("%H:%M")
        match = stop["match"]
        return {
            "station": stop["station"],
            "station_id": station["id"] if station else stop.get("station_id"),
            "planned": planned,
            "arr": station["arr"] if station else None,
            "dep": (station["dep"] if station and station["dep"] else planned),
            "platform": (match or {}).get("platform")
            or (station["platform"] if station else None)
            or stop["platform"],
            "at": stop["planned"].isoformat(timespec="minutes"),
            "source": "plan",
            "course_delay": station["delay"] if station else None,
            "delay": _live_or_course(match, station["delay"] if station else None),
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
            rows.append(anchor_row(anchors[positions.index(i)], station))
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
                "station_id": station["id"],
                "planned": at.strftime("%H:%M") if at else None,
                "arr": station["arr"],
                "dep": station["dep"] or (at.strftime("%H:%M") if at else None),
                "platform": station["platform"],
                "at": at.isoformat(timespec="minutes") if at else None,
                "source": source,
                "course_delay": station["delay"],
                "delay": station["delay"],
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


def _index_relaxed(trips: list[dict]) -> dict:
    """(line, station id, planned minute, None) -> [(trip, stop)], for
    matching without the direction id."""
    index = defaultdict(list)
    for trip in trips:
        for stop in trip["stops"]:
            index[(trip["line"], stop["station_id"], stop["planned"], None)].append(
                (trip, stop)
            )
    return index


def _index_by_service(trips: list[dict]) -> dict:
    """(serviceId, station id) -> [(trip, stop)]: live departures usually carry
    the plan's own serviceId."""
    index: dict[tuple, list] = defaultdict(list)
    for trip in trips:
        for stop in trip["stops"]:
            index[(trip["service_id"], stop["station_id"])].append((trip, stop))
    return index


def _match_by_service(
    by_service_index: dict, entry: dict, station_id: str, planned: datetime
) -> tuple[dict, str] | None:
    """The free plan stop of the entry's own serviceId at this station that
    lies closest in time, within ID_MATCH_WINDOW_MINUTES."""
    window = timedelta(minutes=ID_MATCH_WINDOW_MINUTES)
    free = [
        (abs(stop["planned"] - planned), stop)
        for _, stop in by_service_index.get((str(entry["service_id"]), station_id), [])
        if stop["match"] is None and abs(stop["planned"] - planned) <= window
    ]
    if not free:
        return None
    gap, stop = min(free, key=lambda item: item[0])
    minutes = round(gap.total_seconds() / 60)
    return stop, "ID" if not minutes else f"ID, ±{minutes} min"


def _prefer_platform(free: list, platform: str | None) -> list:
    """Narrow several candidates down to the ones on the live platform."""
    if len(free) > 1 and platform:
        return [(t, s) for t, s in free if s["platform"] == platform] or free
    return free


def alias_course_key(service_id) -> str:
    """Cache key of the departureCourse of a live-only serviceId."""
    return f"live:{service_id}"


def _shared_until(main: list[dict], other: list[dict]) -> str | None:
    """Name of the last station up to which two courses run identically
    (same station and arrival; the first station compares departures)."""
    last = None
    for index, (a, b) in enumerate(zip(main, other)):
        same = a["id"] == b["id"] and (
            a["dep"] == b["dep"] if index == 0 else a["arr"] == b["arr"]
        )
        if not same:
            break
        last = a["name"]
    return last


def _shared_from(main: list[dict], other: list[dict]) -> str | None:
    """Name of the first station from which two courses run identically to
    the end (same station and departure; the last compares arrivals) - where
    two train parts have been coupled."""
    first = None
    for a, b in zip(reversed(main), reversed(other)):
        key = "dep" if a["dep"] or b["dep"] else "arr"
        if a["id"] != b["id"] or a[key] != b[key]:
            break
        first = a["name"]
    return first


def _stop_delay(stop: dict) -> int | None:
    return (
        stop["dep_delay"]
        if stop.get("dep") and stop["dep_delay"] is not None
        else (stop.get("arr_delay"))
    )


def _pick_delay(
    first: int | None, first_at: float, second: int | None, second_at: float
) -> tuple[int | None, float]:
    """Delay of one physical train seen by two courses: the newer course
    wins; equal or unknown age falls back to the larger delay."""
    if first is None:
        return second, second_at
    if second is None or first == second:
        return first, max(first_at, second_at)
    if first_at != second_at:
        return (first, first_at) if first_at > second_at else (second, second_at)
    return max(first, second), first_at


def unify_shared_delays(rows: list[dict], main_at: float, aliases: list[dict]):
    """One delay per stop where a split train still runs as one: live values
    win, else the newer course; sets "delay" on route rows and alias stops."""
    by_name = {row["station"]: row for row in rows}
    ages = {id(row): main_at or 0 for row in rows}
    for alias in aliases:
        stops = alias["stops"] = [dict(s) for s in alias["stops"]]
        names = [s["name"] for s in stops]
        until = names.index(alias["shared_until"]) if alias["shared_until"] else -1
        joined = names.index(alias["shared_from"]) if alias["shared_from"] else None
        alias_at = alias.get("fetched_at") or 0
        for index, stop in enumerate(stops):
            own = _stop_delay(stop)
            stop["delay"] = own
            shared = index <= until or (joined is not None and index >= joined)
            row = by_name.get(stop["name"]) if shared else None
            if row is None:
                continue
            live = row["match"] and row["match"].get("delay")
            if live is not None:
                delay = live
            else:
                delay, ages[id(row)] = _pick_delay(
                    row["delay"], ages[id(row)], own, alias_at
                )
            row["delay"] = stop["delay"] = delay


def _alias_courses(aliases: list[dict], main: dict | None, courses: dict) -> list:
    """Courses of the extra live entries of a trip (e.g. the second half of a
    split train), with the station up to which they match the trip's own."""
    result = []
    for alias in aliases:
        course = courses.get(alias_course_key(alias["service_id"]))
        if not course or not course.get("stops"):
            continue
        shared = _shared_until(main["stops"], course["stops"]) if main else None
        joined = _shared_from(main["stops"], course["stops"]) if main else None
        result.append(
            {
                "service_id": alias["service_id"],
                "destination": alias["destination"],
                "shared_until": shared,
                "shared_from": joined,
                "fetched_at": course.get("fetched_at"),
                "stops": course["stops"],
            }
        )
    return result


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
        "vehicles": vehicles_text(vehicles, _is_ubahn(line)),
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
    """Plan trips matched against the live departures:
    {"summary", "trips", "live_only"}. A live departure matches the plan
    stop with the same line, station, direction id and planned minute; a
    plan trip is "nicht gesehen" if one of its stops lay inside the live
    window but no live departure matched any of its stops."""
    live_anchor = parse_anchor(live.get("anchor"))
    if plan_anchor is None or live_anchor is None:
        return {"summary": {}, "trips": [], "live_only": []}

    trips = _plan_trips(by_service, plan_anchor)
    index = _index_plan(trips)
    by_service_index = _index_by_service(trips)
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
            "between": 0,
        }
    )

    def assign(entry: dict, stop: dict, quality: str) -> None:
        quality_counts[quality] += 1
        per_line[entry["line"]]["matched"] += 1
        stop["match"] = {
            **_public(entry),
            "quality": quality,
            "destination_changed": entry["destination"] != stop["direction"],
            "aliases": [],
            "_vehicles": entry["_vehicles"],
        }

    leftovers = []
    placed: dict[tuple, dict] = {}
    for dep in live.get("departures", []):
        line = dep.get("line") or {}
        if not _is_trip_line(line) or dep.get("timeOffset") is None:
            continue
        live_total += 1
        planned = live_anchor + timedelta(minutes=dep["timeOffset"])
        entry = _live_entry(dep, planned)
        per_line[entry["line"]]["live"] += 1
        station_id = (dep.get("station") or {}).get("id")
        by_id = _match_by_service(by_service_index, entry, station_id, planned)
        if by_id:
            stop, quality = by_id
            assign(entry, stop, quality)
            placed[(entry["line"], station_id, planned, entry["platform"])] = stop
            continue
        key = (entry["line"], station_id, planned, dep.get("directionId"))
        candidates, quality = _find_candidates(index, key)
        free = _prefer_platform(
            [(t, s) for t, s in candidates if s["match"] is None], entry["platform"]
        )
        if not free:
            leftovers.append((entry, station_id, planned, bool(candidates)))
            continue
        if len(free) > 1:
            quality = f"{quality}, mehrdeutig"
        assign(entry, free[0][1], quality)
        placed[(entry["line"], station_id, planned, entry["platform"])] = free[0][1]

    # Second pass: a live entry repeating an already matched one (combined
    # destination of a split train), else a unique plan stop that differs
    # only in the direction id.
    relaxed = _index_relaxed(trips)
    for entry, station_id, planned, had_candidates in leftovers:
        twin = placed.get((entry["line"], station_id, planned, entry["platform"]))
        if twin is not None and entry["platform"]:
            twin["match"]["aliases"].append(
                {
                    "service_id": str(entry["service_id"]),
                    "station_id": station_id,
                    "destination": entry["destination"],
                    "station": entry["station"],
                    "planned": entry["planned"],
                }
            )
            quality_counts["Duplikat"] += 1
            per_line[entry["line"]]["matched"] += 1
            continue
        found, quality = _find_candidates(
            relaxed, (entry["line"], station_id, planned, None)
        )
        free = _prefer_platform(
            [(t, s) for t, s in found if s["match"] is None], entry["platform"]
        )
        if len(free) == 1 and not entry["extra"]:
            assign(entry, free[0][1], f"{quality}, ohne Richtung")
            continue
        live_only.append(
            {
                **_public(entry),
                "reason": "Zusatzfahrt (extra)"
                if entry["extra"]
                else "mehrdeutig"
                if len(free) > 1
                else "Plan-Halt schon vergeben"
                if had_candidates
                else "ohne Plan-Treffer",
            }
        )

    rows = []
    for trip in trips:
        matched = [s for s in trip["stops"] if s["match"]]
        expected = [
            s for s in trip["stops"] if live_anchor <= s["planned"] <= window_end
        ]
        running = (
            trip["stops"][0]["planned"] <= live_anchor <= trip["stops"][-1]["planned"]
        )
        if not matched and not expected and not running:
            continue
        aliases = list(
            {
                a["service_id"]: a for s in matched for a in s["match"]["aliases"]
            }.values()
        )
        cancelled = bool(matched) and all(s["match"]["cancelled"] for s in matched)
        if cancelled:
            status = STATUS_CANCELLED
        else:
            status = (
                STATUS_CONFIRMED
                if matched
                else STATUS_NOT_SEEN
                if expected
                else STATUS_BETWEEN
            )
        vehicles = [v for s in matched for v in s["match"]["_vehicles"]]
        unique = list({(v.get("id"), v.get("number")): v for v in vehicles}.values())
        delays = [
            s["match"]["delay"] for s in matched if s["match"]["delay"] is not None
        ]
        relevant = (
            matched
            or expected
            or [s for s in trip["stops"] if s["planned"] >= live_anchor]
            or trip["stops"]
        )
        rows.append(
            {
                "id": trip["id"],
                "service_id": trip["service_id"],
                "line": trip["line"],
                "line_id": (trip["line_obj"] or {}).get("id", ""),
                "mode": _mode_label(trip["line_obj"]),
                "direction_id": trip["direction_id"],
                "destination": trip["destination"],
                "status": status,
                "start_time": trip["stops"][0]["planned"].strftime("%H:%M"),
                "start_station": trip["stops"][0]["station"],
                "window_time": relevant[0]["planned"].strftime("%H:%M"),
                "window_station": relevant[0]["station"],
                "live_ids": sorted(
                    {str(s["match"]["service_id"]) for s in matched}
                    | {a["service_id"] for s in matched for a in s["match"]["aliases"]}
                ),
                "aliases": aliases,
                "course_fetched_at": ((courses or {}).get(course_key(trip)) or {}).get(
                    "fetched_at"
                ),
                "alias_courses": _alias_courses(
                    aliases, (courses or {}).get(course_key(trip)), courses or {}
                ),
                "destination_changed": any(
                    s["match"]["destination_changed"] for s in matched
                ),
                "max_delay": max(delays) if delays else None,
                "vehicles": vehicles_text(unique, _is_ubahn(trip["line_obj"])),
                "model": refined_model(trip["line_obj"], unique),
                "route_start": None,
                "route_end": None,
                "stops": _route_rows(
                    trip, live_anchor, window_end, (courses or {}).get(course_key(trip))
                ),
                "_sort": trip["stops"][0]["planned"],
            }
        )
        unify_shared_delays(
            rows[-1]["stops"], rows[-1]["course_fetched_at"], rows[-1]["alias_courses"]
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
            "between": sum(r["status"] == STATUS_BETWEEN for r in rows),
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
