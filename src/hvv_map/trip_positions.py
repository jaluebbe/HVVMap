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
    track between stops."""
    if position.state == AT_STOP:
        station = stations.get(ids[position.index] or "")
        return ((station.lon, station.lat), False) if station else None
    start, end = ids[position.index], ids[position.end_index]
    if not start or not end:
        return None
    track = _track(start, end, stations, shapes_conn, cache)
    if track is None:
        return None
    points, distances, straight = track
    point = point_at_distance(points, distances, position.progress * distances[-1])
    return point, straight


def _unit_numbers(row: dict) -> str:
    """U-Bahn unit numbers, e.g. "388 · 340" from "DT5.1 (388), DT5.1 (340)"."""
    if row["mode"] != "U":
        return ""
    return " · ".join(re.findall(r"\((\d+)\)", row.get("vehicles") or ""))


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
    before and after are cut, the train does not run there."""
    kept = [i for i, stop in enumerate(stops) if not stop.get("cancelled")]
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
    on its own course stays. meta: feature -> (direction id, destination,
    confirmed, on own course)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in features:
        if id(f) in meta:
            groups[(_spot(f), meta[id(f)][0])].append(f)
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
        if row["status"] in (STATUS_CANCELLED, STATUS_MISSING) or id(row) in skip:
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
        "features": _one_per_train(features, meta),
    }


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
    due = time.time() - state.recorded >= RECORD_INTERVAL
    if due or arrived:
        if state.units_conn is None:
            state.units_conn = open_units_db()
        if due:
            state.recorded = time.time()
            record_unit_positions(
                state.units_conn, collection["features"], state.recorded
            )
        else:
            record_unit_positions(state.units_conn, arrived, time.time())
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
