"""Compare the planned trips (hvv:departures:by_service) with the
realtime departures (hvv:departures:live): which live entry belongs to which
planned trip, and which planned trips the live window does not show.

Pure functions over the cached payloads, so the api image needs no GTI client.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hvv_map.departure_pairing import pair
from hvv_map.gtilive_geojson import mode_for_line
from hvv_map.departures_tree import (
    _is_ubahn,
    _natural_key,
    _station_name,
    parse_anchor,
    refined_model,
    vehicles_text,
)

# keep in sync with departures_live
LIVE_WINDOW_MINUTES = 5
LIVE_WINDOW_LONG_MINUTES = 20  # regional trains and replacement buses
LONG_WINDOW_MODES = {"R", "SEV"}
GAP_THRESHOLD_MINUTES = 120  # same as departures.GAP_THRESHOLD_MINUTES


STATUS_CONFIRMED = "bestätigt"
STATUS_NOT_SEEN = "nicht gesehen"
STATUS_CANCELLED = "ausgefallen"
STATUS_BETWEEN = "zwischen Ankern"  # running, no anchor stop in the live window
STATUS_MISSING = "vermutlich ausgefallen"  # expected in the live window, never seen
# Only where live coverage is dense, a trip absent from the live window is
# taken as not running - and only while the live data is fresh.
MISSING_MODES = {"U", "S"}
LIVE_FRESH_MINUTES = 3
MISSING_PREFIX = "missing:"  # memory key prefix, see remember_missing


def berlin_now() -> datetime:
    """Current Berlin wall-clock time, naive like the plan and live anchors."""
    return datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)


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
    STATUS_MISSING: "missing",
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
    if trip.get("live_only"):
        return trip["id"]
    return f"{trip['service_id']}@{trip['stops'][0]['planned']:%Y-%m-%dT%H:%M}"


def trip_end(trip: dict, course: dict | None) -> datetime:
    """Last known time of a trip: its last anchor stop or, if later, the end
    of its cached departureCourse."""
    end = trip["stops"][-1]["planned"]
    for stop in (course or {}).get("stops") or []:
        for key in ("dep", "arr"):
            if stop.get(key):
                end = max(end, datetime.fromisoformat(stop[key]))
    return end


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
                    "cancelled": bool(stop.get("cancelled")),
                    "known": True,  # served or not, from the course
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
            "cancelled": False,
            "known": False,
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


def _anchor_positions(anchors: list[dict], stations: list[dict]) -> dict[int, int]:
    """Station index -> anchor index for the anchor stops found in the
    station sequence, in order; anchors it does not contain are skipped."""
    positions: dict[int, int] = {}
    start = 0
    for k, stop in enumerate(anchors):
        found = next(
            (
                i
                for i in range(start, len(stations))
                if stations[i]["id"] == stop["station_id"]
            ),
            None,
        )
        if found is not None:
            positions[found] = k
            start = found + 1
    return positions


def _window_end(trip: dict, live_anchor: datetime) -> datetime:
    """End of the live window that covers the trip's line."""
    long_window = _mode_label(trip["line_obj"] or {}) in LONG_WINDOW_MODES
    minutes = LIVE_WINDOW_LONG_MINUTES if long_window else LIVE_WINDOW_MINUTES
    return live_anchor + timedelta(minutes=minutes)


def _route_rows(
    trip: dict, live_anchor: datetime, window_end: datetime, course: dict | None
) -> tuple[list[dict], list[str]]:
    """(rows, anchors outside the sequence). Full station sequence of a trip,
    from its departureCourse if cached, else its subline. Anchor stops keep
    their planned time and live match; other stations take the course time
    (source "course"), else a time interpolated linearly between the
    neighbouring anchors (source "interpolated"), else none. Anchors the
    sequence does not contain are left out; if none fits, the anchor stops
    are shown alone."""
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
            "cancelled": bool((match or {}).get("cancelled"))
            or bool(station and station["cancelled"]),
            # whether a live departure or the course tells if it is served
            "known": bool(match) or bool(station and station["known"]),
            "match": _public(stop["match"]) if stop["match"] else None,
        }

    if not positions:
        return [anchor_row(stop) for stop in anchors], []

    outside = [
        stop["station"] for k, stop in enumerate(anchors) if k not in positions.values()
    ]
    placed = sorted(positions.items())
    rows = []
    for i, station in enumerate(stations):
        if i in positions:
            rows.append(anchor_row(anchors[positions[i]], station))
            continue
        at, source = None, None
        if station["time"] is not None:
            at, source = station["time"], "course"
        else:
            for (lo, k0), (hi, k1) in zip(placed, placed[1:]):
                if lo < i < hi:
                    t0, t1 = anchors[k0]["planned"], anchors[k1]["planned"]
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
                "cancelled": station["cancelled"],
                "known": station["known"],
                "match": None,
            }
        )
    return rows, outside


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


def carry_live_delay(rows: list[dict], course: dict | None, live_anchor: datetime):
    """Behind the last stop with a live delay, a course older than the live
    data gives no delay: the live one carries on (trip_position.apply_delay)."""
    if not course or not course.get("fetched_at"):
        return
    fetched = datetime.fromtimestamp(course["fetched_at"], ZoneInfo("Europe/Berlin"))
    if fetched.replace(tzinfo=None) >= live_anchor:
        return
    last = max(
        (
            i
            for i, row in enumerate(rows)
            if row["match"] and row["match"].get("delay") is not None
        ),
        default=None,
    )
    if last is None:
        return
    for row in rows[last + 1 :]:
        row["delay"] = None


def _vehicle_fields(
    trip: dict, unique: list, memory: dict | None, course: dict | None = None
) -> dict:
    """Vehicles text and model of a trip: from its live entries, else from the
    vehicles of its departureCourse, else what was remembered while it had none
    (e.g. between anchors)."""
    ubahn = _is_ubahn(trip["line_obj"])
    text = vehicles_text(unique, ubahn)
    if text:
        return {
            "vehicles": text,
            "model": refined_model(trip["line_obj"], unique),
            "vehicles_remembered": False,
            "vehicles_from_course": False,
        }
    from_course = (course or {}).get("vehicles") or []
    if from_course:
        return {
            "vehicles": vehicles_text(from_course, ubahn),
            "model": refined_model(trip["line_obj"], from_course),
            "vehicles_remembered": False,
            "vehicles_from_course": True,
        }
    remembered = (memory or {}).get(course_key(trip))
    if remembered:
        return {
            "vehicles": remembered["vehicles"],
            "model": remembered["model"],
            "vehicles_remembered": True,
            "vehicles_from_course": False,
        }
    return {
        "vehicles": "",
        "model": refined_model(trip["line_obj"], unique),
        "vehicles_remembered": False,
        "vehicles_from_course": False,
    }


def _course_failed(course: dict | None) -> bool:
    """A fetch was tried but gave no stops (GTI error or a discarded answer)."""
    return course is not None and not course.get("stops")


COMBINED_DESTINATION = " / "  # a coupled train lists both destinations
COURSE_FIT_MINUTES = 10  # a course belongs to a trip if its times are this close
# Lines whose announced destination may be missing from the course; elsewhere
# announcements and course stop names differ in spelling only.
COURSE_DESTINATION_LINES = {"S1"}


def destination_from_courses(
    announced: list[str | None],
    stop_lists: list[list[dict]],
    now: datetime | None = None,
) -> str | None:
    """The announced destinations (split at " / ") that one of the courses
    still has ahead (departure not before `now`, or its last stop), in order;
    if none is left, the courses' last stops."""
    names = {
        stop["name"]
        for stops in stop_lists
        for stop in stops
        if not stop.get("dep")
        or now is None
        or datetime.fromisoformat(stop["dep"]) >= now
    }
    parts = [
        part for text in announced if text for part in text.split(COMBINED_DESTINATION)
    ]
    kept = list(dict.fromkeys(part for part in parts if part in names))
    if not kept:
        kept = list(dict.fromkeys(stops[-1]["name"] for stops in stop_lists))
    return COMBINED_DESTINATION.join(kept)


def course_destination(
    announced: str | None, course: dict | None, line: str, now: datetime | None = None
) -> str | None:
    """On COURSE_DESTINATION_LINES the announced destinations the course
    contains, else the course's last stop (see destination_from_courses)."""
    stops = (course or {}).get("stops")
    if line not in COURSE_DESTINATION_LINES or not stops or not announced:
        return announced
    return destination_from_courses([announced], [stops], now)


def _course_fits(trip: dict, course: dict) -> bool:
    """A serviceId repeats and is reused: the course must contain one of the
    trip's anchors, and pass every anchor it contains at the anchor's time."""
    tolerance = timedelta(minutes=COURSE_FIT_MINUTES)
    times: dict[str, list[datetime]] = defaultdict(list)
    for stop in course["stops"]:
        for key in ("dep", "arr"):
            if stop.get(key):
                times[stop["id"]].append(datetime.fromisoformat(stop[key]))
    contained = [stop for stop in trip["stops"] if stop["station_id"] in times]
    return bool(contained) and all(
        any(abs(at - stop["planned"]) <= tolerance for at in times[stop["station_id"]])
        for stop in contained
    )


def _trip_course(
    trip: dict, live_ids: list[str], courses: dict
) -> tuple[str | None, dict | None]:
    """(live serviceId, course) of the first of the trip's live ids with a
    cached course that fits the trip; a failed fetch counts as found."""
    for service_id in live_ids:
        course = courses.get(alias_course_key(service_id))
        if course is not None and (
            not course.get("stops") or _course_fits(trip, course)
        ):
            return service_id, course
    return None, None


def _alias_courses(
    trip: dict,
    aliases: list[dict],
    main: dict | None,
    courses: dict,
    now: datetime | None = None,
) -> list:
    """Courses of the extra live entries of a trip (e.g. the second half of a
    split train), with the station up to which they match the trip's own."""
    result = []
    ubahn = _is_ubahn(trip["line_obj"])
    main_stops = (main or {}).get("stops")  # None for a failed course fetch
    for alias in aliases:
        course = courses.get(alias_course_key(alias["service_id"]))
        if not course or not course.get("stops") or not _course_fits(trip, course):
            continue
        shared = _shared_until(main_stops, course["stops"]) if main_stops else None
        joined = _shared_from(main_stops, course["stops"]) if main_stops else None
        result.append(
            {
                "service_id": alias["service_id"],
                "destination": course_destination(
                    alias["destination"], course, trip["line"], now
                ),
                "destination_announced": alias["destination"],
                "shared_until": shared,
                "shared_from": joined,
                "fetched_at": course.get("fetched_at"),
                "vehicles": vehicles_text(course.get("vehicles"), ubahn),
                "stops": course["stops"],
            }
        )
    return result


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
        "planned_platform": dep.get("platform"),
        "delay": dep.get("delay"),
        "cancelled": bool(dep.get("cancelled")),
        "extra": bool(dep.get("extra")),
        "vehicles": vehicles_text(vehicles, _is_ubahn(line)),
        "model": refined_model(line, vehicles),
        "_vehicles": vehicles or [],
        "_line": line,
    }


def _public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


REDIS_VEHICLES_KEY = "hvv:trips:vehicles"  # written by trip_positions.py
VEHICLE_MEMORY_SECONDS = 6 * 3600  # a trip's vehicles are forgotten after this


def remember_vehicles(memory: dict, table: dict, now_ts: float) -> bool:
    """Store the vehicles of the table's trips that have some, keyed by
    course_key, and drop old entries. True if `memory` changed."""
    changed = False
    for row in table["trips"]:
        if not row["vehicles"] or row["vehicles_remembered"]:
            continue
        entry = memory.get(row["course_key"])
        if not entry or (entry["vehicles"], entry["model"]) != (
            row["vehicles"],
            row["model"],
        ):
            memory[row["course_key"]] = {
                "vehicles": row["vehicles"],
                "model": row["model"],
                "seen_at": now_ts,
            }
            changed = True
    for key in [
        key
        for key, entry in memory.items()
        if now_ts - entry["seen_at"] > VEHICLE_MEMORY_SECONDS
    ]:
        del memory[key]
        changed = True
    return changed


LIVE_TRIP_PREFIX = "livetrip:"  # id prefix of live-only trips
LIVE_TRIP_MEMORY_PREFIX = "trip:"  # their key prefix in the vehicle memory
LIVE_TRIP_QUALITY = "nur live"
LIVE_TRIP_KEEP_MINUTES = 3  # a live-only trip is shown this long past its end
LIVE_TRIP_MEMORY_SECONDS = 6 * 3600


def _live_trip_stop(entry: dict, station_id: str, planned: datetime) -> dict:
    return {
        "station_id": station_id,
        "station": entry["station"],
        "direction": entry["destination"],
        "platform": entry["platform"],
        "planned": planned,
        "match": {
            **_public(entry),
            "quality": LIVE_TRIP_QUALITY,
            "destination_changed": False,
            "aliases": [],
            "_vehicles": entry["_vehicles"],
        },
    }


def _live_trip_key(trips: dict, entry: dict, planned: datetime) -> str:
    """The key of the trip with the entry's serviceId that has a stop close in
    time, else a new one named by the service day."""
    close = timedelta(minutes=GAP_THRESHOLD_MINUTES)
    for key, trip in trips.items():
        if trip["service_id"] == str(entry["service_id"]) and any(
            abs(s["planned"] - planned) <= close for s in trip["stops"]
        ):
            return key
    day = (planned - timedelta(hours=4)).date()
    return f"{LIVE_TRIP_PREFIX}{entry['service_id']}@{day}"


def _merge_live_trips(found: list[tuple], remembered: dict) -> list[dict]:
    """Live-only trips: one per serviceId and service day, with the live stops
    seen so far. `found` is [(entry, station id, planned)] of this snapshot,
    `remembered` the earlier trips from memory (see remember_live_trips)."""
    trips: dict[str, dict] = {}
    for memory_key, saved in remembered.items():
        if not memory_key.startswith(LIVE_TRIP_MEMORY_PREFIX):
            continue
        trips[memory_key[len(LIVE_TRIP_MEMORY_PREFIX) :]] = {
            **saved["trip"],
            "stops": [
                {**s, "planned": datetime.fromisoformat(s["planned"])}
                for s in saved["trip"]["stops"]
            ],
        }
    for entry, station_id, planned in found:
        key = _live_trip_key(trips, entry, planned)
        trip = trips.setdefault(
            key,
            {
                "id": key,
                "service_id": str(entry["service_id"]),
                "line": entry["line"],
                "line_obj": entry["_line"],
                "direction_id": entry["direction_id"],
                "destination": entry["destination"],
                "subline": None,
                "live_only": True,
                "stops": [],
            },
        )
        trip["destination"] = entry["destination"]
        trip["stops"] = [s for s in trip["stops"] if s["station_id"] != station_id] + [
            _live_trip_stop(entry, station_id, planned)
        ]
        trip["stops"].sort(key=lambda s: s["planned"])
    return list(trips.values())


def remember_live_trips(memory: dict, table: dict, now_ts: float) -> bool:
    """Store the table's live-only trips (see build_trip_table) in `memory`
    and drop old ones. True if `memory` changed."""
    changed = False
    for key, trip in table.get("live_trips", {}).items():
        memory_key = LIVE_TRIP_MEMORY_PREFIX + key
        if memory.get(memory_key, {}).get("trip") != trip:
            memory[memory_key] = {"trip": trip, "seen_at": now_ts}
            changed = True
    for key in [
        key
        for key, entry in memory.items()
        if key.startswith(LIVE_TRIP_MEMORY_PREFIX)
        and now_ts - entry["seen_at"] > LIVE_TRIP_MEMORY_SECONDS
    ]:
        del memory[key]
        changed = True
    return changed


def _serialized_live_trip(trip: dict) -> dict:
    return {
        **{k: v for k, v in trip.items() if k != "stops"},
        "stops": [{**s, "planned": s["planned"].isoformat()} for s in trip["stops"]],
    }


def in_repeated_hour(moment: datetime) -> bool:
    """True for a naive Berlin time the autumn DST change makes ambiguous;
    a departureList query for it does not answer for our wall clock."""
    tz = ZoneInfo("Europe/Berlin")
    return (
        moment.replace(tzinfo=tz, fold=0).utcoffset()
        > moment.replace(tzinfo=tz, fold=1).utcoffset()  # spring gap: <
    )


def _is_missing(trip: dict, status: str, live_fresh: bool, memory: dict | None) -> bool:
    """A trip whose anchor stop was due in the live window but never showed
    up there while the live data is fresh; or one already judged so earlier
    (until it shows up live again)."""
    if _mode_label(trip["line_obj"]) not in MISSING_MODES:
        return False
    if MISSING_PREFIX + course_key(trip) in (memory or {}):
        return True
    return status == STATUS_NOT_SEEN and live_fresh


def remember_missing(memory: dict, table: dict, now_ts: float) -> bool:
    """Remember trips judged missing, so they stay so once their expected
    stop has left the live window; forget those seen live after all (e.g.
    late). True if `memory` changed."""
    changed = False
    for row in table["trips"]:
        key = MISSING_PREFIX + row["course_key"]
        if row["status"] == STATUS_MISSING and key not in memory:
            memory[key] = {"seen_at": now_ts}
            changed = True
        elif row["status"] == STATUS_CONFIRMED and key in memory:
            del memory[key]
            changed = True
    return changed


LIVE_IDS_PREFIX = "ids:"  # memory key prefix, see remember_live_ids


def remember_live_ids(memory: dict, table: dict, now_ts: float) -> bool:
    """Remember the live serviceIds, wing parts and announced destination of
    the table's matched trips, so their courses stay known between anchors.
    True if `memory` changed."""
    changed = False
    for row in table["trips"]:
        if not row.get("primary_ids"):
            continue
        key = LIVE_IDS_PREFIX + row["course_key"]
        saved = {
            "ids": row["primary_ids"],
            "aliases": row["aliases"],
            "destination": row["destination_announced"],
        }
        entry = memory.get(key)
        if not entry or {k: entry.get(k) for k in saved} != saved:
            memory[key] = {**saved, "seen_at": now_ts}
            changed = True
    return changed


def _pairing_plan(trip: dict, stop: dict) -> dict:
    return {
        "at": stop["planned"],
        "platform": stop["platform"],
        "direction": stop["direction"],
        "direction_id": trip["direction_id"],
    }


def _pairing_live(entry: dict, planned: datetime) -> dict:
    return {
        "at": planned,
        "platform": entry["planned_platform"] or entry["platform"],
        "direction": entry["destination"],
        "direction_id": entry["direction_id"],
    }


def build_trip_table(
    by_service: dict,
    plan_anchor: datetime | None,
    live: dict,
    courses: dict | None = None,
    vehicle_memory: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Plan trips matched against the live departures:
    {"summary", "trips", "live_only", "live_trips"}. Plan and live departures
    are paired per line and station (see departure_pairing); a wing train's
    second live departure becomes an alias of the matched stop; unpaired
    live departures form live-only trips. A plan trip is "nicht gesehen" if
    one of its stops lay inside the live window but none was paired.
    Courses are looked up by live serviceId (alias_course_key)."""
    live_anchor = parse_anchor(live.get("anchor"))
    if plan_anchor is None or live_anchor is None:
        return {"summary": {}, "trips": [], "live_only": []}
    courses = courses or {}
    memory = vehicle_memory or {}

    trips = _plan_trips(by_service, plan_anchor)
    live_only, quality_counts, live_total = [], defaultdict(int), 0
    live_fresh = now is None or now - live_anchor <= timedelta(
        minutes=LIVE_FRESH_MINUTES
    )
    # live data of the repeated autumn hour is unreliable: judge nothing missing
    judge_missing = not in_repeated_hour(live_anchor)
    per_line: dict[str, dict] = defaultdict(
        lambda: {
            "live": 0,
            "matched": 0,
            "trips": 0,
            "confirmed": 0,
            "not_seen": 0,
            "cancelled": 0,
            "between": 0,
            "missing": 0,
        }
    )

    plan_at: dict[tuple, list] = defaultdict(list)
    for trip in trips:
        for stop in trip["stops"]:
            plan_at[(trip["line"], stop["station_id"])].append((trip, stop))
    live_at: dict[tuple, list] = defaultdict(list)
    for dep in live.get("departures", []):
        line = dep.get("line") or {}
        if not _is_trip_line(line) or dep.get("timeOffset") is None:
            continue
        live_total += 1
        planned = live_anchor + timedelta(minutes=dep["timeOffset"])
        entry = _live_entry(dep, planned)
        per_line[entry["line"]]["live"] += 1
        station_id = (dep.get("station") or {}).get("id")
        live_at[(entry["line"], station_id)].append((entry, planned))

    live_found: list[tuple] = []
    for (line_name, station_id), lives in live_at.items():
        plans = sorted(
            plan_at.get((line_name, station_id), []), key=lambda ts: ts[1]["planned"]
        )
        extras = [i for i, (entry, _) in enumerate(lives) if entry["extra"]]
        regular = [i for i, (entry, _) in enumerate(lives) if not entry["extra"]]
        rows, free = pair(
            [_pairing_plan(trip, stop) for trip, stop in plans],
            [_pairing_live(*lives[i]) for i in regular],
            line_name,
        )
        rows = [
            (p, None if l is None else regular[l], None if w is None else regular[w], q)
            for p, l, w, q in rows
        ]
        free = [regular[i] for i in free] + extras
        for p, l, wing, quality in rows:
            if l is None:
                continue
            entry, stop = lives[l][0], plans[p][1]
            quality_counts[quality] += 1
            per_line[line_name]["matched"] += 1
            stop["match"] = {
                **_public(entry),
                "quality": quality,
                "destination_changed": entry["destination"] != stop["direction"],
                "aliases": [],
                "_vehicles": entry["_vehicles"],
            }
            if wing is not None:
                other, other_planned = lives[wing]
                stop["match"]["aliases"].append(
                    {
                        "service_id": str(other["service_id"]),
                        "station_id": station_id,
                        "destination": other["destination"],
                        "station": other["station"],
                        "planned": other["planned"],
                    }
                )
                stop["match"]["_vehicles"] = entry["_vehicles"] + other["_vehicles"]
                quality_counts["Flügel"] += 1
                per_line[line_name]["matched"] += 1
        for l in free:
            entry, planned = lives[l]
            live_found.append((entry, station_id, planned))
            if not entry["extra"] and str(entry["service_id"]) in by_service:
                live_only.append({**_public(entry), "reason": "Plan-ID ohne Paarung"})

    # Live departures without a plan partner become trips of their own,
    # unless their serviceId now belongs to a plan trip (also as wing part).
    paired = {
        str(service_id)
        for trip in trips
        for stop in trip["stops"]
        if stop["match"]
        for service_id in [stop["match"]["service_id"]]
        + [a["service_id"] for a in stop["match"]["aliases"]]
    }
    for key, entry in memory.items():
        if key.startswith(LIVE_IDS_PREFIX):
            paired |= set(entry["ids"]) | {a["service_id"] for a in entry["aliases"]}
    live_trips = _merge_live_trips(live_found, memory)
    live_trips = [
        t
        for t in live_trips
        if t["service_id"] not in paired
        and trip_end(t, _trip_course(t, [t["service_id"]], courses)[1])
        + timedelta(minutes=LIVE_TRIP_KEEP_MINUTES)
        >= live_anchor
    ]
    trips = trips + live_trips

    rows = []
    for trip in trips:
        matched = [s for s in trip["stops"] if s["match"]]
        primary_ids = list(
            dict.fromkeys(str(s["match"]["service_id"]) for s in matched)
        )
        aliases = list(
            {
                a["service_id"]: a for s in matched for a in s["match"]["aliases"]
            }.values()
        )
        announced = (
            matched[-1]["match"]["destination"] if matched else trip["destination"]
        )
        remembered = memory.get(LIVE_IDS_PREFIX + course_key(trip)) or {}
        if not matched and remembered:
            aliases = remembered["aliases"]
            announced = remembered["destination"] or announced
        if trip.get("live_only"):
            course_id, course = _trip_course(trip, [trip["service_id"]], courses)
        else:
            course_id, course = _trip_course(
                trip, primary_ids or remembered.get("ids") or [], courses
            )
        window_end = _window_end(trip, live_anchor)
        expected = [
            s for s in trip["stops"] if live_anchor <= s["planned"] <= window_end
        ]
        running = trip["stops"][0]["planned"] <= live_anchor <= trip_end(trip, course)
        if not matched and not expected and not running:
            continue
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
            if (
                judge_missing
                and not matched
                and _is_missing(trip, status, live_fresh, memory)
            ):
                status = STATUS_MISSING
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
        stops, outside = _route_rows(trip, live_anchor, window_end, course)
        carry_live_delay(stops, course, live_anchor)
        alias_courses = _alias_courses(trip, aliases, course, courses, live_anchor)
        rows.append(
            {
                "id": trip["id"],
                "service_id": trip["service_id"],
                "line": trip["line"],
                "line_id": (trip["line_obj"] or {}).get("id", ""),
                "mode": _mode_label(trip["line_obj"]),
                "direction_id": trip["direction_id"],
                "destination": course_destination(
                    announced, course, trip["line"], live_anchor
                ),
                "destination_announced": announced,
                "status": status,
                "start_time": trip["stops"][0]["planned"].strftime("%H:%M"),
                "start_station": trip["stops"][0]["station"],
                "window_time": relevant[0]["planned"].strftime("%H:%M"),
                "window_station": relevant[0]["station"],
                "live_ids": sorted(
                    set(primary_ids) | {a["service_id"] for a in aliases}
                ),
                "primary_ids": primary_ids,
                "aliases": aliases,
                "course_id": course_id,
                "course_fetched_at": (course or {}).get("fetched_at"),
                "course_failed": _course_failed(course),
                "anchors_outside_course": (
                    outside if (course or {}).get("stops") else []
                ),
                "alias_courses": alias_courses,
                "destination_changed": any(
                    s["match"]["destination_changed"] for s in matched
                ),
                "max_delay": max(delays) if delays else None,
                "course_key": course_key(trip),
                "live_only": bool(trip.get("live_only")),
                **_vehicle_fields(trip, unique, memory, course),
                "route_start": None,
                "route_end": None,
                "stops": stops,
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
            "missing": sum(r["status"] == STATUS_MISSING for r in rows),
            "extra": sum(e["extra"] for e in live_only),
            "by_line": {
                name: per_line[name] for name in sorted(per_line, key=_natural_key)
            },
            "live_anchor": live_anchor.strftime("%Y-%m-%d %H:%M"),
            "plan_anchor": plan_anchor.strftime("%Y-%m-%d %H:%M"),
        },
        "trips": rows,
        "live_only": live_only,
        "live_trips": {t["id"]: _serialized_live_trip(t) for t in live_trips},
    }
