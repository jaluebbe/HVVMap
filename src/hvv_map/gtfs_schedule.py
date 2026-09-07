"""Parse the static HVV GTFS feed into an in-memory schedule and locate a
trip's current position along it.

No GTI credentials, no live API, no segment cache: the moving vehicle marker
still interpolates along a straight line between two consecutive stops (see
head_position()) - shapes.txt would need reliable stop-to-shape matching for
that (shape_dist_traveled isn't guaranteed present/accurate), left as a
future improvement. The reference LINES layer, however, draws each trip's
full shapes.txt polyline directly (see gtfs_geojson.build_lines_geojson) -
no matching needed there, since trips.txt links a trip to its shape_id
directly.

calendar_dates.txt exceptions ARE respected here, unlike hvv_gtfs.py, which
explicitly ignored them.
"""

import csv
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

csv.field_size_limit(10_000_000)

WEEKDAY_FIELDS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

# route_type -> mode (U/S/AKN/FERRY), specific to HVV's GTFS feed.
MODE_BY_ROUTE_TYPE = {"109": "S", "402": "U", "2": "AKN", "1200": "FERRY"}
MODEL_BY_ROUTE_TYPE = {
    "109": "S-Bahn",
    "402": "U-Bahn",
    "2": "Regionalbahn",
    "1200": "Fähre",
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
}


def _mode_for_route(route: dict) -> str:
    if route["route_id"] in SPECIAL_BUS_ROUTES:
        return SPECIAL_BUS_ROUTES[route["route_id"]]
    return MODE_BY_ROUTE_TYPE.get(route["route_type"], "")


def _is_route_of_interest(row: dict) -> bool:
    if row["route_id"] in SPECIAL_BUS_ROUTES:
        return True
    if row["route_type"] in ("109", "402"):
        return True
    if row["route_type"] == "2" and row["route_short_name"] in ("A1", "A2", "A3"):
        return True
    if row["route_type"] == "1200" and row["route_short_name"] not in ("5160", "5170"):
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
    calendar_rows: list
    calendar_dates: dict  # (service_id, date) -> exception_type ("1" added, "2" removed)
    shapes: dict = field(default_factory=dict)  # shape_id -> [(lon, lat), ...], only for used trips
    _active_services_cache: dict = field(default_factory=dict, repr=False)


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

    with open(gtfs_path / "calendar.txt", encoding="utf-8") as f:
        calendar_rows = list(csv.DictReader(f))

    calendar_dates = {}
    calendar_dates_path = gtfs_path / "calendar_dates.txt"
    if calendar_dates_path.is_file():
        with open(calendar_dates_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                exception_date = datetime.strptime(row["date"], "%Y%m%d").date()
                calendar_dates[(row["service_id"], exception_date)] = row["exception_type"]

    # Only the shapes actually used by a trip of interest are kept - the
    # full feed's shapes.txt also covers the regional bus network and can be
    # very large, so unrelated rows are skipped while streaming instead of
    # loaded and discarded afterwards.
    needed_shape_ids = {trip["shape_id"] for trip in trips.values() if trip.get("shape_id")}
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
        calendar_rows=calendar_rows,
        calendar_dates=calendar_dates,
        shapes=shapes,
    )


def mode_for_trip(schedule: Schedule, trip: dict) -> str:
    return _mode_for_route(schedule.routes[trip["route_id"]])


def active_service_ids(schedule: Schedule, target_date: date) -> set:
    """Weekday pattern from calendar.txt, adjusted by calendar_dates.txt
    exceptions (exception_type 1 adds a service for that date, 2 removes
    it)."""
    cached = schedule._active_services_cache.get(target_date)
    if cached is not None:
        return cached

    weekday_field = WEEKDAY_FIELDS[target_date.weekday()]
    active = set()
    for row in schedule.calendar_rows:
        start = datetime.strptime(row["start_date"], "%Y%m%d").date()
        end = datetime.strptime(row["end_date"], "%Y%m%d").date()
        if start <= target_date <= end and row[weekday_field] == "1":
            active.add(row["service_id"])

    for (service_id, exception_date), exception_type in schedule.calendar_dates.items():
        if exception_date != target_date:
            continue
        if exception_type == "1":
            active.add(service_id)
        elif exception_type == "2":
            active.discard(service_id)

    schedule._active_services_cache[target_date] = active
    return active


# Mirrors the live API path's STALE_THRESHOLD_SECONDS (segment_cache.py),
# but computed directly from the known static schedule instead of an
# organically-grown observation cache - the full recent calendar is already
# known in advance, so nothing needs to be "learned" from live sightings.
STALE_THRESHOLD_DAYS = 6


def recently_active_service_ids(
    schedule: Schedule, reference_date: date, lookback_days: int = STALE_THRESHOLD_DAYS
) -> set:
    """Union of active_service_ids() for reference_date and each of the
    lookback_days before it."""
    active = set()
    for offset in range(lookback_days + 1):
        active |= active_service_ids(schedule, reference_date - timedelta(days=offset))
    return active


def recently_active_shape_ids(
    schedule: Schedule, reference_date: date, lookback_days: int = STALE_THRESHOLD_DAYS
) -> set:
    """shape_ids used by at least one trip whose service ran on
    reference_date or within lookback_days before it - e.g. a diversion's
    shape disappears from the reference lines layer once its service has
    been gone for that long, same as the live API path."""
    service_ids = recently_active_service_ids(schedule, reference_date, lookback_days)
    shape_ids = set()
    for trip in schedule.trips.values():
        shape_id = trip.get("shape_id")
        if shape_id and trip["service_id"] in service_ids and shape_id in schedule.shapes:
            shape_ids.add(shape_id)
    return shape_ids


def find_current_segment(rows: list, target_seconds: int):
    """rows: one trip's [(seq, stop_id, arrival_s, departure_s), ...].
    Returns the (prev, nxt) pair target_seconds currently falls into, using
    each stop's arrival time as the lower bound so a train stays visible
    during its station dwell time - or None if target_seconds is outside
    the trip's overall time span."""
    if not rows or not (rows[0][2] <= target_seconds < rows[-1][2]):
        return None
    for prev, nxt in zip(rows, rows[1:]):
        if prev[2] <= target_seconds < nxt[2]:
            return prev, nxt
    return None


def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    earth_radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius * math.asin(math.sqrt(a))


def _cumulative_distances(coords: list) -> list:
    dists = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        dists.append(dists[-1] + _haversine(lon1, lat1, lon2, lat2))
    return dists


def _point_at_distance(coords: list, dists: list, target_dist: float):
    if target_dist <= 0:
        return coords[0]
    if target_dist >= dists[-1]:
        return coords[-1]
    for i in range(1, len(dists)):
        if dists[i] >= target_dist:
            seg_len = dists[i] - dists[i - 1]
            if seg_len == 0:
                return coords[i]
            frac = (target_dist - dists[i - 1]) / seg_len
            lon1, lat1 = coords[i - 1]
            lon2, lat2 = coords[i]
            return (lon1 + frac * (lon2 - lon1), lat1 + frac * (lat2 - lat1))
    return coords[-1]


def head_position(coords: list, actual_start: int, actual_end: int, now_ts: int):
    """Progress (0..1) and interpolated (lon, lat) along coords at now_ts,
    clamped to the segment's own time span. coords takes 2+ points so this
    keeps working unchanged if shapes.txt-based multi-point geometry
    replaces the current straight-line pairs later. None if coords is too
    short to interpolate along."""
    if len(coords) < 2:
        return None
    if actual_end <= actual_start:
        progress = 1.0
    else:
        progress = (now_ts - actual_start) / (actual_end - actual_start)
        progress = max(0.0, min(1.0, progress))

    dists = _cumulative_distances(coords)
    target_dist = progress * dists[-1]
    head = _point_at_distance(coords, dists, target_dist)
    return progress, head
