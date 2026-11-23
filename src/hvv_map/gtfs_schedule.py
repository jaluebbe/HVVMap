"""Parse the static HVV GTFS feed (routes, stops, trips, stop times and
shapes of the modes shown) into an in-memory schedule, for gtfs_tracks."""

import csv
from dataclasses import dataclass, field
from pathlib import Path

from hvv_map.gtfs_lines import FERRY_AGENCY
from hvv_map.lines import R_BAHN_LINES

csv.field_size_limit(10_000_000)

# route_type -> mode (U/S/AKN/FERRY), specific to HVV's GTFS feed.
MODE_BY_ROUTE_TYPE = {"109": "S", "402": "U", "1200": "FERRY"}

# route_type "2" is shared by otherwise-unrelated modes in this feed - AKN
# (A1/A2/A3) and RB81 (interim solution ahead of the future S4) - so
# route_short_name decides the actual mode, same idea as SPECIAL_BUS_ROUTES
# below for route_type "3". Single source of truth for both the interest
# filter and the mode lookup.
ROUTE_TYPE_2_MODE_BY_SHORT_NAME = {
    "A1": "AKN",
    "A2": "AKN",
    "A3": "AKN",
    **dict.fromkeys(R_BAHN_LINES, "R"),
}

# Replacement buses (route_type=3, normally excluded) shown grouped with the
# line they replace. route_id -> forced mode. Confirmed against the live GTI
# API's listLines, ported from the earlier hvv_gtfs.py experiment.
SPECIAL_BUS_ROUTES = {
    "13881_3": "U",
    "8956_3": "U",  # U1-DIREKT, U1-ERSATZ
    "14131_3": "AKN",
    "13071_3": "AKN",
    "11796_3": "AKN",  # A1-SEV, A2-SEV, A3-Bus
    "14476_3": "S",  # S5-SEV
    "11710_3": "R",  # RB61-SEV
    "14841_3": "R",  # RB71-SEV
    "11334_3": "R",  # RE80-SEV
}


def _mode_for_route(route: dict) -> str:
    if route["route_id"] in SPECIAL_BUS_ROUTES:
        return SPECIAL_BUS_ROUTES[route["route_id"]]
    if route["route_type"] == "2":
        return ROUTE_TYPE_2_MODE_BY_SHORT_NAME.get(route["route_short_name"], "")
    return MODE_BY_ROUTE_TYPE.get(route["route_type"], "")


def _is_route_of_interest(row: dict) -> bool:
    if row["route_id"] in SPECIAL_BUS_ROUTES:
        return True
    if row["route_type"] in ("109", "402"):
        return True
    if (
        row["route_type"] == "2"
        and row["route_short_name"] in ROUTE_TYPE_2_MODE_BY_SHORT_NAME
    ):
        return True
    # Harbour ferries only; the rural Elbe ferries run under other agencies.
    if row["route_type"] == "1200" and row.get("agency_id") == FERRY_AGENCY:
        return True
    return False


def _parse_gtfs_time_to_seconds(value: str) -> int:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


@dataclass
class Schedule:
    routes: dict  # route_id -> row
    stops: dict  # stop_id -> row
    trips: dict  # trip_id -> row
    trip_stop_times: dict  # trip_id -> [(seq, stop_id, arrival_s, departure_s), ...]
    shapes: dict = field(
        default_factory=dict
    )  # shape_id -> [(lon, lat), ...], only for used trips


def load_schedule(gtfs_dir: str) -> Schedule:
    gtfs_path = Path(gtfs_dir)

    routes = {}
    with open(gtfs_path / "routes.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if _is_route_of_interest(row):
                routes[row["route_id"]] = row

    stops = {}
    with open(gtfs_path / "stops.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            stops[row["stop_id"]] = row

    trips = {}
    with open(gtfs_path / "trips.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["route_id"] in routes:
                trips[row["trip_id"]] = row

    trip_stop_times: dict = {}
    with open(gtfs_path / "stop_times.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            trip_id = row["trip_id"]
            if trip_id not in trips:
                continue
            trip_stop_times.setdefault(trip_id, []).append(
                (
                    int(row["stop_sequence"]),
                    row["stop_id"],
                    _parse_gtfs_time_to_seconds(row["arrival_time"]),
                    _parse_gtfs_time_to_seconds(row["departure_time"]),
                )
            )
    for rows in trip_stop_times.values():
        rows.sort(key=lambda r: r[0])

    # Only the shapes actually used by a trip of interest are kept - the
    # full feed's shapes.txt also covers the regional bus network and can be
    # very large, so unrelated rows are skipped while streaming instead of
    # loaded and discarded afterwards.
    needed_shape_ids = {
        trip["shape_id"] for trip in trips.values() if trip.get("shape_id")
    }
    shapes: dict = {}
    shapes_path = gtfs_path / "shapes.txt"
    if shapes_path.is_file() and needed_shape_ids:
        raw_points: dict = {}
        with open(shapes_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                shape_id = row["shape_id"]
                if shape_id not in needed_shape_ids:
                    continue
                raw_points.setdefault(shape_id, []).append(
                    (
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lon"]),
                        float(row["shape_pt_lat"]),
                    )
                )
        for shape_id, points in raw_points.items():
            points.sort(key=lambda p: p[0])
            shapes[shape_id] = [(lon, lat) for _, lon, lat in points]

    return Schedule(
        routes=routes,
        stops=stops,
        trips=trips,
        trip_stop_times=trip_stop_times,
        shapes=shapes,
    )


def mode_for_trip(schedule: Schedule, trip: dict) -> str:
    return _mode_for_route(schedule.routes[trip["route_id"]])
