"""Fallback tracks from the static GTFS feed for hops GTI delivers no
geometry for (stored as a two-point straight link). Only rail modes; the
GTFS shape of a trip serving both stations is cut at the stations."""

from __future__ import annotations

import os
from dataclasses import dataclass

from hvv_map.geometry import haversine
from hvv_map.gti_shapes import (
    apply_derived_sublines,
    clip_track,
    is_real_track,
    store_segment,
    subline_hops,
)
from hvv_map.gtilive_geojson import _SHORT_INFO_MODE as SHORT_INFO_MODE
from hvv_map.gtfs_schedule import Schedule, load_schedule, mode_for_trip

GTFS_DIR = os.environ.get("GTFS_DIR", "data/gtfs")
RAIL_MODES = {"U", "S", "AKN", "R"}
STOP_MATCH_M = 400.0  # GTFS stop within this distance of a GTI station
MAX_DETOUR = 2.5  # accepted track length relative to the straight distance
DETOUR_SLACK_M = 500.0


@dataclass
class GtfsIndex:
    shapes: dict  # shape_id -> [(lon, lat), ...]
    shapes_by_stop: dict  # (mode, stop_id) -> {shape_id, ...}
    stops_by_mode: dict  # mode -> [(stop_id, lon, lat), ...]


def build_index(schedule: Schedule) -> GtfsIndex:
    shapes_by_stop: dict = {}
    for trip_id, rows in schedule.trip_stop_times.items():
        trip = schedule.trips[trip_id]
        shape_id = trip.get("shape_id")
        if shape_id not in schedule.shapes:
            continue
        mode = mode_for_trip(schedule, trip)
        if mode not in RAIL_MODES:
            continue
        for _seq, stop_id, _arrival, _departure in rows:
            shapes_by_stop.setdefault((mode, stop_id), set()).add(shape_id)
    stops_by_mode: dict = {mode: [] for mode in RAIL_MODES}
    for mode, stop_id in shapes_by_stop:
        stop = schedule.stops.get(stop_id)
        if stop:
            stops_by_mode[mode].append(
                (stop_id, float(stop["stop_lon"]), float(stop["stop_lat"]))
            )
    used = set().union(*shapes_by_stop.values()) if shapes_by_stop else set()
    return GtfsIndex(
        {sid: schedule.shapes[sid] for sid in used}, shapes_by_stop, stops_by_mode
    )


def _shapes_near(index: GtfsIndex, mode: str, lon: float, lat: float) -> set:
    shape_ids: set = set()
    for stop_id, stop_lon, stop_lat in index.stops_by_mode.get(mode, ()):
        if (
            abs(stop_lat - lat) < 0.01
            and abs(stop_lon - lon) < 0.02
            and haversine(lon, lat, stop_lon, stop_lat) <= STOP_MATCH_M
        ):
            shape_ids |= index.shapes_by_stop[(mode, stop_id)]
    return shape_ids


def _length(path: list) -> float:
    return sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(path, path[1:]))


def hop_track(index: GtfsIndex, mode: str, start: tuple, end: tuple) -> list | None:
    """Shortest sane cut of a GTFS shape serving both points."""
    shape_ids = _shapes_near(index, mode, *start) & _shapes_near(index, mode, *end)
    limit = MAX_DETOUR * haversine(*start, *end) + DETOUR_SLACK_M
    best = None
    for shape_id in sorted(shape_ids):
        part = clip_track([list(p) for p in index.shapes[shape_id]], start, end)
        if part and _length(part) <= limit and (best is None or len(part) < len(best)):
            best = part
    return best


def fill_from_index(conn, by_service: dict, stations: dict, index: GtfsIndex) -> int:
    """Store GTFS tracks for every rail hop of a resolved subline that has no
    real track; returns the number of hops filled."""
    wanted: set = set()
    for entry in apply_derived_sublines(conn, by_service).values():
        subline = entry.get("subline")
        mode = SHORT_INFO_MODE.get((entry["line"].get("type") or {}).get("shortInfo"))
        if not subline or mode not in RAIL_MODES:
            continue
        ids = tuple(stop["id"] for stop in subline)
        for a, b, segment in subline_hops(conn, ids):
            if not is_real_track(segment):
                wanted.add((mode, a, b))
    filled = 0
    for mode, a, b in sorted(wanted):
        from_station, to_station = stations.get(a), stations.get(b)
        if from_station is None or to_station is None:
            continue
        track = hop_track(
            index,
            mode,
            (from_station.lon, from_station.lat),
            (to_station.lon, to_station.lat),
        )
        if track:
            store_segment(conn, a, b, track)
            filled += 1
    return filled


_cache: dict = {}


def fill_from_gtfs(
    conn, by_service: dict, stations: dict, gtfs_dir: str | None = None
) -> int:
    """fill_from_index with the feed at gtfs_dir (GTFS_DIR); 0 if there is none.
    The index is rebuilt only when the feed changes."""
    routes = os.path.join(gtfs_dir or GTFS_DIR, "routes.txt")
    if not os.path.isfile(routes):
        return 0
    mtime = os.path.getmtime(routes)
    if _cache.get("mtime") != mtime:
        _cache["index"] = build_index(load_schedule(os.path.dirname(routes)))
        _cache["mtime"] = mtime
    return fill_from_index(conn, by_service, stations, _cache["index"])
