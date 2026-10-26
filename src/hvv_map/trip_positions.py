"""Vehicle positions from the trip table: every running trip is located with
trip_position.locate() and placed on its station or along the stored track
of its current segment."""

import json
import re
from collections import defaultdict
import traceback
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from hvv_map.departures import REDIS_ANCHOR_KEY, REDIS_BY_SERVICE_KEY, berlin_now
from hvv_map.departures_live import REDIS_LIVE_KEY
from hvv_map.departures_tree import parse_anchor
from hvv_map.geometry import cumulative_distances, point_at_distance
from hvv_map.gti_shapes import open_db as open_shapes_db
from hvv_map.gti_shapes import is_real_track, subline_hops
from hvv_map.gtilive_geojson import _subline_coordinates
from hvv_map.lines import replacement_bus_mode
from hvv_map.redis_client import get_redis_client
from hvv_map.stations import StationInfo, by_id, load_stations
from hvv_map.trip_courses import REDIS_COURSES_KEY
from hvv_map.unit_positions import RECORD_INTERVAL, record_unit_positions
from hvv_map.unit_positions import open_db as open_units_db
from hvv_map.trip_position import AT_STOP, BETWEEN, Position, locate, stops_from_row
from hvv_map.trips import (
    COMBINED_DESTINATION,
    REDIS_VEHICLES_KEY,
    STATUS_CANCELLED,
    STATUS_MISSING,
    build_trip_table,
    destination_from_courses,
    remember_confirmed,
    remember_live_ids,
    remember_live_trips,
    remember_missing,
    remember_vehicles,
)

ICON_HEIGHT = 10
REDIS_POSITIONS_KEY = "hvv:positions_gtilive"
POSITIONS_TTL = 120
VEHICLES_TTL = 24 * 3600
POSITIONS_INTERVAL = 1.0  # seconds between recomputations
ERROR_LOG_INTERVAL = 60.0  # seconds before an identical error is logged again
RELOAD_INTERVAL = 10.0  # seconds between checks for new source data


def _track(start, end, stations, shapes_conn, cache):
    """(points, cumulative distances, straight) of a segment, memoized in
    `cache`; straight = no stored track, just the line between stations."""
    key = (start, end)
    if key not in cache:
        coordinates = _subline_coordinates((start, end), stations, shapes_conn)
        points = [tuple(c) for c in coordinates]
        if len(points) < 2:
            cache[key] = None
        else:
            hops = subline_hops(shapes_conn, (start, end))
            real = any(is_real_track(segment) for _a, _b, segment in hops)
            cache[key] = (points, cumulative_distances(points), not real)
    return cache[key]


def _coordinates(
    position: Position,
    ids: list[str | None],
    stations: dict[str, StationInfo],
    shapes_conn: sqlite3.Connection | None,
    cache: dict,
) -> tuple[tuple[float, float], bool] | None:
    """(point, straight): the station at a stop, a point on the segment's
    track between stops. A stop missing from `stations` (e.g. a platform id)
    is placed at the end of the stored track leading to it."""
    if position.state == AT_STOP:
        index = position.index
        station = stations.get(ids[index] or "")
        if station:
            return (station.lon, station.lat), False
        if index > 0 and ids[index - 1] and ids[index]:
            track = _track(ids[index - 1], ids[index], stations, shapes_conn, cache)
            if track:
                return track[0][-1], track[2]
        if index + 1 < len(ids) and ids[index] and ids[index + 1]:
            track = _track(ids[index], ids[index + 1], stations, shapes_conn, cache)
            if track:
                return track[0][0], track[2]
        return None
    start, end = ids[position.index], ids[position.end_index]
    if not start or not end:
        return None
    track = _track(start, end, stations, shapes_conn, cache)
    if track is None:
        return None
    points, distances, straight = track
    point = point_at_distance(points, distances, position.progress * distances[-1])
    return point, straight


def _numbers(vehicles: str | None) -> list[str]:
    """Unit numbers from a vehicles text like "DT5.1 (388), DT5.1 (340)"."""
    return re.findall(r"\((\d+)\)", vehicles or "")


def _dropped_units(row: dict) -> dict[str, dict]:
    """Units a live departure of the trip listed but its latest one no longer
    does (uncoupled on the way): unit -> the last stop that listed it."""
    listed = [
        s for s in row["stops"] if _numbers((s.get("match") or {}).get("vehicles"))
    ]
    if len(listed) < 2:
        return {}
    latest = set(_numbers(listed[-1]["match"]["vehicles"]))
    dropped = {}
    for stop in listed[:-1]:
        for unit in _numbers(stop["match"]["vehicles"]):
            if unit not in latest:
                dropped[unit] = stop
    return dropped


def _unit_numbers(row: dict) -> str:
    """U-Bahn unit numbers, e.g. "388 · 340", without uncoupled ones."""
    if row["mode"] != "U":
        return ""
    dropped = _dropped_units(row)
    return " · ".join(u for u in _numbers(row.get("vehicles")) if u not in dropped)


def _feature(
    row: dict,
    destination: str,
    position: Position,
    point,
    delay: int | None,
    straight: bool,
) -> dict:
    mode = row["mode"]
    if mode == "SEV":
        mode = replacement_bus_mode(row["line"])
    minutes = round((delay or 0) / 60)
    # second line: the U-Bahn units if known, else the vehicle model
    detail = (row.get("vehicles") if row["mode"] == "U" else None) or row.get("model")
    text = f"{destination}<br>{detail}" if detail else destination
    if minutes > 0:
        text = f"{text}<br>+{minutes} Minuten"
    icon = (
        f"https://cloud.geofox.de/icon/line?height={ICON_HEIGHT}"
        f"&lineKey={row['line_id']}&fileFormat=SVG"
    )
    if mode == "R":
        icon += "&outlined=true"
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": list(point)},
        "properties": {
            "journeyID": row["id"],
            "icon": icon,
            "iconHeight": ICON_HEIGHT,
            "line": row["line"],
            "label": destination.split("(")[0].strip(),
            "text": text,
            "progress": round(position.progress or 0, 3),
            "mode": mode,
            "delay": minutes,
            "straight": straight,
            "units": _unit_numbers(row),
        },
    }


def _served(stops: list[dict]) -> list[dict]:
    """Stops from the first to the last one the train serves; cancelled stops
    before and after are cut, the train does not run there. After the first
    cancelled stop, a stop counts as served only if known to be (live or
    course): GTI flags only the departures it lists, e.g. a short turn."""
    first = next((i for i, s in enumerate(stops) if s.get("cancelled")), len(stops))
    kept = [
        i
        for i, stop in enumerate(stops)
        if not stop.get("cancelled") and (i < first or stop.get("known", True))
    ]
    return stops[kept[0] : kept[-1] + 1] if kept else []


def _served_row(row: dict) -> dict | None:
    """The row and its wing courses cut to their served stops, ending at the
    last served stop instead of the announced destination; None if no stop
    is served."""
    stops = _served(row["stops"])
    if not stops:
        return None
    aliases = []
    for alias in row["alias_courses"]:
        served = _served(alias["stops"])
        if served:
            cut = served[-1] is not alias["stops"][-1]
            destination = served[-1]["name"] if cut else alias["destination"]
            aliases.append({**alias, "stops": served, "destination": destination})
    cut = stops[-1] is not row["stops"][-1]
    return {
        **row,
        "stops": stops,
        "alias_courses": aliases,
        "destination": stops[-1]["station"] if cut else row["destination"],
        "destination_announced": (
            stops[-1]["station"] if cut else row.get("destination_announced")
        ),
    }


def _alias_stops(alias: dict) -> list[dict]:
    return [
        {
            "name": s["name"],
            "arr": datetime.fromisoformat(s["arr"]) if s.get("arr") else None,
            "dep": datetime.fromisoformat(s["dep"]) if s.get("dep") else None,
            "delay": s.get("delay"),
        }
        for s in alias["stops"]
    ]


def _is_shared(alias: dict, position: Position) -> bool:
    """The position lies in a section the alias course shares with the trip."""
    names = [s["name"] for s in alias["stops"]]
    until = names.index(alias["shared_until"]) if alias["shared_until"] else -1
    joined = names.index(alias["shared_from"]) if alias["shared_from"] else len(names)

    def shared(index):
        return index <= until or index >= joined

    ends = (
        [position.index, position.end_index] if position.end_index else [position.index]
    )
    return all(shared(i) for i in ends)


def _label_destination(row: dict, now: datetime, ferry: bool) -> str:
    """While a coupled train runs as one, the announced destinations of both
    parts that their courses contain (e.g. "Poppenbüttel / Hamburg Airport");
    if none is left, the courses' last stops."""
    if not row.get("course_id") or row.get("course_failed"):
        return row["destination"]
    for alias in row["alias_courses"]:
        position = locate(_alias_stops(alias), now, ferry)
        if (
            position
            and position.state in (AT_STOP, BETWEEN)
            and _is_shared(alias, position)
        ):
            last = len(row["stops"]) - 1
            own = [
                {"name": s["station"], "dep": s["at"] if i < last else None}
                for i, s in enumerate(row["stops"])
            ]
            return destination_from_courses(
                [alias.get("destination_announced"), row.get("destination_announced")],
                [own, alias["stops"]],
                now,
            )
    return row["destination"]


def _drawn_by_others(trips: list[dict], now: datetime) -> tuple[set, set]:
    """(rows to skip, wing ids not to draw): one train part is drawn once.
    Skipped are a trip without a current live departure whose course belongs
    to another trip's live id, and a trip whose live id is another trip's own
    serviceId (one vehicle, two plan trips). A trip whose live id is another
    trip's wing part is skipped while the coupled train runs as one; after
    the split it draws that part instead of the other trip's wing. Of trips
    sharing one course, only the one whose serviceId it is (else a
    confirmed one) is drawn."""
    wings = {
        alias["service_id"]: (row, alias)
        for row in trips
        for alias in row["alias_courses"]
    }
    primaries = {sid: row for row in trips for sid in row.get("primary_ids") or []}
    owners = defaultdict(list)
    for row in trips:
        for sid in row.get("primary_ids") or []:
            owners[sid].append(row)
    skip, hidden = set(), set()
    for row in trips:
        ids = row.get("primary_ids") or []
        if not ids:
            course_id = row.get("course_id")
            other = primaries.get(course_id) or (wings.get(course_id) or (None,))[0]
            if other is not None and other is not row:
                skip.add(id(row))
            continue
        if any(
            other is not row and other["service_id"] == sid
            for sid in ids
            for other in owners[sid]
        ):
            # the live id is another trip's own serviceId: that trip draws it
            skip.add(id(row))
            continue
        for service_id in ids:
            owner, alias = wings.get(service_id) or (None, None)
            if owner is None or owner is row:
                continue
            position = locate(_alias_stops(alias), now, owner["mode"] == "FERRY")
            if (
                position
                and position.state in (AT_STOP, BETWEEN)
                and _is_shared(alias, position)
            ):
                skip.add(id(row))
            else:
                hidden.add(service_id)
    by_course = defaultdict(list)
    for row in trips:
        if row.get("course_id") and row["status"] not in (
            STATUS_CANCELLED,
            STATUS_MISSING,
        ):
            by_course[row["course_id"]].append(row)
    for course_id, rows in by_course.items():
        keep = (
            next((r for r in rows if r.get("service_id") == course_id), None)
            or next((r for r in rows if r.get("primary_ids")), None)
            or rows[0]
        )
        skip.update(id(r) for r in rows if r is not keep)
    return skip, hidden


def _spot(feature: dict) -> tuple:
    lon, lat = feature["geometry"]["coordinates"]
    return feature["properties"]["line"], round(lon, 4), round(lat, 4)


def _one_per_train(features: list[dict], meta: dict[int, tuple]) -> list[dict]:
    """Main features of one line and direction at the same spot are one
    train. Confirmed trips win over trips without a current live departure
    (coupled parts after joining, when GTI lists only one of them); several
    confirmed trips are a coupled train (e.g. RE7 to Kiel and Flensburg) and
    get one feature with all destinations; of unconfirmed ones only the trip
    on its own course stays. A trip at its terminus is another train than
    one starting there (e.g. U3 at Barmbek). meta: feature -> (direction id,
    destination, confirmed, on own course)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in features:
        if id(f) in meta:
            terminus = bool(f["properties"].get("terminus"))
            groups[(_spot(f), meta[id(f)][0], terminus)].append(f)
    dropped = set()
    for parts in groups.values():
        if len(parts) < 2:
            continue
        confirmed = [f for f in parts if meta[id(f)][2]]
        if not confirmed:
            keep = next((f for f in parts if meta[id(f)][3]), parts[0])
            dropped.update(id(f) for f in parts if f is not keep)
            continue
        dropped.update(id(f) for f in parts if not meta[id(f)][2])
        first = confirmed[0]
        destinations = list(dict.fromkeys(meta[id(f)][1] for f in confirmed))
        if len(destinations) > 1:
            props = first["properties"]
            combined = COMBINED_DESTINATION.join(destinations)
            props["text"] = props["text"].replace(meta[id(first)][1], combined, 1)
            props["label"] = COMBINED_DESTINATION.join(
                d.split("(")[0].strip() for d in destinations
            )
        dropped.update(id(f) for f in confirmed[1:])
    return [f for f in features if id(f) not in dropped]


def build_positions(
    table: dict,
    now: datetime,
    stations: dict[str, StationInfo],
    shapes_conn: sqlite3.Connection | None = None,
    cache: dict | None = None,
) -> dict:
    """GeoJSON FeatureCollection of the trips running at `now`. A split train
    yields one feature for the shared part and one for the other branch.
    `cache` keeps segment tracks between calls on the same data."""
    cache = {} if cache is None else cache
    features = []
    rows = [r for r in map(_served_row, table["trips"]) if r]
    skip, hidden = _drawn_by_others(rows, now)
    meta: dict[int, tuple] = {}  # see _one_per_train
    for row in rows:
        # a cancelled trip still runs its served part (partial cancellation)
        if row["status"] == STATUS_MISSING or id(row) in skip:
            continue
        ferry = row["mode"] == "FERRY"
        stops = stops_from_row(row)
        position = locate(stops, now, ferry)
        if position and position.state in (AT_STOP, BETWEEN):
            ids = [s["station_id"] for s in row["stops"]]
            placed = _coordinates(position, ids, stations, shapes_conn, cache)
            if placed:
                point, straight = placed
                delay = row["stops"][position.index]["delay"]
                destination = _label_destination(row, now, ferry)
                feature = _feature(row, destination, position, point, delay, straight)
                if position.state == AT_STOP and position.index == len(stops) - 1:
                    feature["properties"]["terminus"] = True
                meta[id(feature)] = (
                    row.get("direction_id"),
                    destination,
                    bool(row.get("primary_ids")),
                    row.get("course_id") == row.get("service_id"),
                )
                features.append(feature)
        for alias in row["alias_courses"]:
            if alias["service_id"] in hidden:
                continue
            position = locate(_alias_stops(alias), now, ferry)
            if (
                not position
                or position.state not in (AT_STOP, BETWEEN)
                or _is_shared(alias, position)
            ):
                continue
            ids = [s["id"] for s in alias["stops"]]
            placed = _coordinates(position, ids, stations, shapes_conn, cache)
            if placed:
                point, straight = placed
                delay = alias["stops"][position.index]["delay"]
                features.append(
                    _feature(
                        {**row, "id": alias["service_id"]},
                        alias["destination"],
                        position,
                        point,
                        delay,
                        straight,
                    )
                )
    return {
        "type": "FeatureCollection",
        "features": _one_feature_per_unit(_one_per_train(features, meta), rows),
    }


def _one_feature_per_unit(features: list[dict], rows: list[dict]) -> list[dict]:
    """A unit on two trips belongs to the one that started later (e.g. turned
    back early as an extra trip); a trip left without units of its own then
    is no train any more."""
    starts = {r["id"]: r.get("route_start") or "" for r in rows}
    owner: dict[str, dict] = {}
    for feature in features:
        start = starts.get(feature["properties"]["journeyID"], "")
        for unit in _units_of(feature):
            other = owner.get(unit)
            if other is None or start > starts.get(
                other["properties"]["journeyID"], ""
            ):
                owner[unit] = feature
    kept = []
    for feature in features:
        units = _units_of(feature)
        own = [u for u in units if owner[u] is feature]
        if units and not own:
            continue
        feature["properties"]["units"] = " · ".join(own)
        kept.append(feature)
    return kept


def _units_of(feature: dict) -> list[str]:
    return [u for u in (feature["properties"].get("units") or "").split(" · ") if u]


def _dropped_features(table: dict, stations: dict[str, StationInfo]) -> list[dict]:
    """One feature per uncoupled unit at the last stop that listed it, for the
    unit positions (see _dropped_units)."""
    features = []
    for row in table.get("trips", []):
        if row["mode"] != "U":
            continue
        for unit, stop in _dropped_units(row).items():
            station = stations.get(stop.get("station_id") or "")
            if station is None:
                continue
            point = (station.lon, station.lat)
            position = Position(AT_STOP)
            feature = _feature(row, stop["station"], position, point, None, False)
            # no trip terminus for it (unit_history): the unit stays here
            feature["properties"]["journeyID"] = f"{row['id']}/uncoupled"
            feature["properties"]["units"] = unit
            features.append(feature)
    return features


class PositionsState:
    """Trip table and segment tracks, rebuilt only when the source data in
    Redis has changed (checked every RELOAD_INTERVAL)."""

    def __init__(self):
        self.raw: tuple | None = None
        self.checked = float("-inf")
        self.table: dict | None = None
        self.stations: dict[str, StationInfo] = {}
        self.tracks: dict = {}
        self.memory: dict | None = None  # trip vehicles, see trips.remember_vehicles
        self.recorded = float("-inf")  # last write of the unit positions
        self.units_conn: sqlite3.Connection | None = None  # see unit_positions
        self.at_terminus: set[str] = set()  # journeys recorded at their terminus
        self.uncoupled: set[tuple] = set()  # (unit, journey) recorded uncoupled


def _refresh(redis_client, state: PositionsState) -> bool:
    """Bring `state` up to date; False without source data."""
    if time.time() - state.checked < RELOAD_INTERVAL and state.table is not None:
        return True
    state.checked = time.time()
    keys = (REDIS_BY_SERVICE_KEY, REDIS_ANCHOR_KEY, REDIS_LIVE_KEY, REDIS_COURSES_KEY)
    raw = tuple(redis_client.get(key) for key in keys)
    plan, anchor, live, courses = raw
    if not (plan and anchor and live):
        return False
    if raw != state.raw:
        if state.memory is None:
            stored = redis_client.get(REDIS_VEHICLES_KEY)
            state.memory = json.loads(stored)["data"] if stored else {}
        state.table = build_trip_table(
            json.loads(plan)["data"],
            parse_anchor(json.loads(anchor)["data"]),
            json.loads(live)["data"],
            json.loads(courses)["data"] if courses else {},
            state.memory,
        )
        now_ts = time.time()
        changed = remember_vehicles(state.memory, state.table, now_ts)
        changed = remember_live_trips(state.memory, state.table, now_ts) or changed
        changed = remember_missing(state.memory, state.table, now_ts) or changed
        changed = remember_live_ids(state.memory, state.table, now_ts) or changed
        changed = remember_confirmed(state.memory, state.table, now_ts) or changed
        if changed:
            redis_client.set(
                REDIS_VEHICLES_KEY,
                json.dumps({"fetched_at": int(time.time()), "data": state.memory}),
                ex=VEHICLES_TTL,
            )
        state.stations = by_id(load_stations(redis_client))
        state.tracks = {}
        state.raw = raw
    return True


def _trip_ends(table: dict, stations: dict[str, StationInfo]) -> dict[str, dict]:
    """journeyID -> last served stop's coordinates and planned time (epoch),
    stored with the unit positions, see unit_positions.unit_history."""
    ends = {}
    for row in table.get("trips", []):
        served = _served_row(row)
        stops = served["stops"] if served else []
        station = stations.get(stops[-1].get("station_id") or "") if stops else None
        times = [s["at"] for s in stops if s.get("at")]
        if station is None or not times:
            continue
        end = datetime.fromisoformat(max(times))
        ends[row["id"]] = {
            "coordinates": [station.lon, station.lat],
            "at": int(end.replace(tzinfo=ZoneInfo("Europe/Berlin")).timestamp()),
        }
    return ends


def run_positions(
    redis_client,
    shapes_conn: sqlite3.Connection | None,
    now: datetime | None = None,
    state: PositionsState | None = None,
) -> int:
    """Compute from the data in Redis and store under REDIS_POSITIONS_KEY;
    returns the number of vehicles. Pass the same `state` on repeated calls."""
    state = state or PositionsState()
    if not _refresh(redis_client, state):
        return 0
    collection = build_positions(
        state.table, now or berlin_now(), state.stations, shapes_conn, state.tracks
    )
    redis_client.set(
        REDIS_POSITIONS_KEY,
        json.dumps({"fetched_at": int(time.time()), "data": collection}),
        ex=POSITIONS_TTL,
    )
    # the terminus dwell can be shorter than RECORD_INTERVAL: record arrivals
    # there at once, so the last known position is the terminus
    terminus = {
        f["properties"]["journeyID"]: f
        for f in collection["features"]
        if f["properties"].get("terminus")
    }
    arrived = [f for jid, f in terminus.items() if jid not in state.at_terminus]
    state.at_terminus = set(terminus)
    # uncoupled units stay where they were last listed, recorded once
    uncoupled = {
        (f["properties"]["units"], f["properties"]["journeyID"]): f
        for f in _dropped_features(state.table, state.stations)
    }
    arrived += [f for key, f in uncoupled.items() if key not in state.uncoupled]
    state.uncoupled = set(uncoupled)
    due = time.time() - state.recorded >= RECORD_INTERVAL
    if due or arrived:
        if state.units_conn is None:
            state.units_conn = open_units_db()
        ends = _trip_ends(state.table, state.stations)
        if due:
            state.recorded = time.time()
            # arrivals and new uncoupled units after the regular positions
            features = collection["features"] + arrived
            record_unit_positions(state.units_conn, features, state.recorded, ends)
        else:
            record_unit_positions(state.units_conn, arrived, time.time(), ends)
    return len(collection["features"])


def run_loop(interval: float = POSITIONS_INTERVAL) -> None:
    """Endless recomputation; opens its own connections (thread-safe use)."""
    redis_client = get_redis_client()
    shapes_conn = open_shapes_db()
    state = PositionsState()
    last_error = ("", 0.0)
    while True:
        started = time.time()
        try:
            run_positions(redis_client, shapes_conn, state=state)
        except Exception:  # keep the loop alive across bad data
            message = traceback.format_exc()
            if (
                message != last_error[0]
                or started - last_error[1] >= ERROR_LOG_INTERVAL
            ):
                print(f"[hvvmap-trip-positions] {message}", flush=True)
                last_error = (message, started)
        time.sleep(max(0.0, interval - (time.time() - started)))


def main() -> None:
    """Compute once and print the number of vehicles."""
    conn = open_shapes_db()
    try:
        print(run_positions(get_redis_client(), conn))
    finally:
        conn.close()
