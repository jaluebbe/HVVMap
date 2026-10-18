"""Vehicle positions from the trip table: every running trip is located with
trip_position.locate() and placed on its station or along the stored track
of its current segment. Same GeoJSON properties as geojson.py."""

import json
import sqlite3
import time
from datetime import datetime

from hvv_map.departures import REDIS_ANCHOR_KEY, REDIS_BY_SERVICE_KEY, berlin_now
from hvv_map.departures_live import REDIS_LIVE_KEY
from hvv_map.departures_tree import parse_anchor
from hvv_map.geojson import ICON_HEIGHT
from hvv_map.gti_shapes import open_db as open_shapes_db
from hvv_map.gti_shapes import subline_hops
from hvv_map.gtilive_geojson import _subline_coordinates
from hvv_map.lines import replacement_bus_mode
from hvv_map.positions import _cumulative_distances, _point_at_distance
from hvv_map.redis_client import get_redis_client
from hvv_map.stations import StationInfo, by_id, load_stations
from hvv_map.trip_courses import REDIS_COURSES_KEY
from hvv_map.trip_position import AT_STOP, BETWEEN, Position, locate, stops_from_row
from hvv_map.trips import (
    REDIS_VEHICLES_KEY,
    STATUS_CANCELLED,
    build_trip_table,
    remember_vehicles,
)

REDIS_POSITIONS_KEY = "hvv:positions_gtilive"
POSITIONS_TTL = 120
VEHICLES_TTL = 24 * 3600
POSITIONS_INTERVAL = 1.0  # seconds between recomputations
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
            real = any(segment and len(segment) > 2 for _a, _b, segment in hops)
            cache[key] = (points, _cumulative_distances(points), not real)
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
    point = _point_at_distance(points, distances, position.progress * distances[-1])
    return point, straight


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
        },
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
    for row in table["trips"]:
        if row["status"] == STATUS_CANCELLED:
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
                features.append(
                    _feature(row, row["destination"], position, point, delay, straight)
                )
        for alias in row["alias_courses"]:
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
    return {"type": "FeatureCollection", "features": features}


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
        if remember_vehicles(state.memory, state.table, time.time()):
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
    return len(collection["features"])


def run_loop(interval: float = POSITIONS_INTERVAL) -> None:
    """Endless recomputation; opens its own connections (thread-safe use)."""
    redis_client = get_redis_client()
    shapes_conn = open_shapes_db()
    state = PositionsState()
    while True:
        started = time.time()
        try:
            run_positions(redis_client, shapes_conn, state=state)
        except Exception as error:  # keep the loop alive across bad data
            print(f"[hvvmap-trip-positions] {error!r}", flush=True)
        time.sleep(max(0.0, interval - (time.time() - started)))


def main() -> None:
    """Compute once and print the number of vehicles."""
    conn = open_shapes_db()
    try:
        print(run_positions(get_redis_client(), conn))
    finally:
        conn.close()
