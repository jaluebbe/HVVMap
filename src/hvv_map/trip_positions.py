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
from hvv_map.gtilive_geojson import _subline_coordinates
from hvv_map.lines import replacement_bus_mode
from hvv_map.positions import _cumulative_distances, _point_at_distance
from hvv_map.redis_client import get_redis_client
from hvv_map.stations import StationInfo, by_id, load_stations
from hvv_map.trip_courses import REDIS_COURSES_KEY
from hvv_map.trip_position import AT_STOP, BETWEEN, Position, locate, stops_from_row
from hvv_map.trips import STATUS_CANCELLED, build_trip_table

REDIS_POSITIONS_KEY = "hvv:positions_gtilive"
POSITIONS_TTL = 120
POSITIONS_INTERVAL = 10.0  # seconds between recomputations


def _along(coordinates: list, progress: float) -> tuple[float, float]:
    points = [tuple(c) for c in coordinates]
    distances = _cumulative_distances(points)
    return _point_at_distance(points, distances, progress * distances[-1])


def _coordinates(
    position: Position,
    ids: list[str | None],
    stations: dict[str, StationInfo],
    shapes_conn: sqlite3.Connection | None,
) -> tuple[float, float] | None:
    """Station coordinate at a stop, point on the segment's track between."""
    if position.state == AT_STOP:
        station = stations.get(ids[position.index] or "")
        return (station.lon, station.lat) if station else None
    start, end = ids[position.index], ids[position.end_index]
    if not start or not end:
        return None
    track = _subline_coordinates((start, end), stations, shapes_conn)
    return _along(track, position.progress) if len(track) >= 2 else None


def _feature(
    row: dict, destination: str, position: Position, point, delay: int | None
) -> dict:
    mode = row["mode"]
    if mode == "SEV":
        mode = replacement_bus_mode(row["line"])
    minutes = round((delay or 0) / 60)
    text = f"{destination}<br>{row['model']}" if row.get("model") else destination
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
) -> dict:
    """GeoJSON FeatureCollection of the trips running at `now`. A split train
    yields one feature for the shared part and one for the other branch."""
    features = []
    for row in table["trips"]:
        if row["status"] == STATUS_CANCELLED:
            continue
        ferry = row["mode"] == "FERRY"
        stops = stops_from_row(row)
        position = locate(stops, now, ferry)
        if position and position.state in (AT_STOP, BETWEEN):
            ids = [s["station_id"] for s in row["stops"]]
            point = _coordinates(position, ids, stations, shapes_conn)
            if point:
                delay = row["stops"][position.index]["delay"]
                features.append(
                    _feature(row, row["destination"], position, point, delay)
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
            point = _coordinates(position, ids, stations, shapes_conn)
            if point:
                delay = alias["stops"][position.index]["delay"]
                features.append(
                    _feature(
                        {**row, "id": alias["service_id"]},
                        alias["destination"],
                        position,
                        point,
                        delay,
                    )
                )
    return {"type": "FeatureCollection", "features": features}


def run_positions(
    redis_client, shapes_conn: sqlite3.Connection | None, now: datetime | None = None
) -> int:
    """Compute from the data in Redis and store under REDIS_POSITIONS_KEY;
    returns the number of vehicles."""
    now = now or berlin_now()
    raw_plan = redis_client.get(REDIS_BY_SERVICE_KEY)
    raw_anchor = redis_client.get(REDIS_ANCHOR_KEY)
    raw_live = redis_client.get(REDIS_LIVE_KEY)
    if not (raw_plan and raw_anchor and raw_live):
        return 0
    raw_courses = redis_client.get(REDIS_COURSES_KEY)
    courses = json.loads(raw_courses)["data"] if raw_courses else {}
    table = build_trip_table(
        json.loads(raw_plan)["data"],
        parse_anchor(json.loads(raw_anchor)["data"]),
        json.loads(raw_live)["data"],
        courses,
    )
    stations = by_id(load_stations(redis_client))
    collection = build_positions(table, now, stations, shapes_conn)
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
    while True:
        started = time.time()
        try:
            run_positions(redis_client, shapes_conn)
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
