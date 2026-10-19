"""All bus shapes of the static GTFS feed as one simplified GeoJSON layer,
for the developer map only (static/dev_bus_tracks.html). Tracks only - no
trips, no positions."""

from __future__ import annotations

import csv
import math
from pathlib import Path

csv.field_size_limit(10_000_000)

TOLERANCE_M = 5.0  # simplification tolerance
PRECISION = 5  # decimals of the output coordinates (~1 m)
M_PER_DEG_LAT = 111_200.0


def is_bus_route_type(route_type: str) -> bool:
    """Basic type 3 or the extended bus types 700-799."""
    return route_type == "3" or (route_type.isdigit() and 700 <= int(route_type) <= 799)


def _distance_m(point, start, end, lon_scale: float) -> float:
    """Distance of point from the segment start-end in meters (planar)."""
    px, py = (point[0] - start[0]) * lon_scale, (point[1] - start[1]) * M_PER_DEG_LAT
    ex, ey = (end[0] - start[0]) * lon_scale, (end[1] - start[1]) * M_PER_DEG_LAT
    length_sq = ex * ex + ey * ey
    if length_sq == 0:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * ex + py * ey) / length_sq))
    return math.hypot(px - t * ex, py - t * ey)


def simplify(points: list, tolerance_m: float = TOLERANCE_M) -> list:
    """Douglas-Peucker on [(lon, lat), ...]; the end points are kept."""
    if len(points) < 3:
        return list(points)
    lon_scale = M_PER_DEG_LAT * math.cos(math.radians(points[0][1]))
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        worst, index = 0.0, None
        for i in range(first + 1, last):
            d = _distance_m(points[i], points[first], points[last], lon_scale)
            if d > worst:
                worst, index = d, i
        if index is not None and worst > tolerance_m:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep) if k]


def _bus_routes(gtfs_path: Path) -> dict[str, str]:
    with open(gtfs_path / "routes.txt", encoding="utf-8") as f:
        return {
            row["route_id"]: row.get("route_short_name") or row["route_id"]
            for row in csv.DictReader(f)
            if is_bus_route_type(row["route_type"])
        }


def _lines_by_shape(gtfs_path: Path, routes: dict[str, str]) -> dict[str, set]:
    lines: dict[str, set] = {}
    with open(gtfs_path / "trips.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["route_id"] in routes and row.get("shape_id"):
                lines.setdefault(row["shape_id"], set()).add(routes[row["route_id"]])
    return lines


def _simplified_shapes(gtfs_path: Path, wanted: set):
    """(shape_id, simplified points) per wanted shape. shapes.txt is read
    once; a shape is simplified as soon as its rows end, so only one shape's
    raw points are in memory (rows of a shape are expected to be adjacent)."""
    current, rows = None, []

    def finish():
        rows.sort()
        return current, simplify([(lon, lat) for _seq, lon, lat in rows])

    with open(gtfs_path / "shapes.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            shape_id = row["shape_id"]
            if shape_id != current:
                if rows:
                    yield finish()
                current, rows = shape_id, []
            if shape_id in wanted:
                rows.append(
                    (
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lon"]),
                        float(row["shape_pt_lat"]),
                    )
                )
    if rows:
        yield finish()


def build_bus_tracks_geojson(gtfs_dir: str, tolerance_m: float = TOLERANCE_M) -> dict:
    """One LineString per distinct bus track, properties.lines = the line
    names using it (properties.search: the same, lowercase, for filtering)."""
    gtfs_path = Path(gtfs_dir)
    lines_by_shape = _lines_by_shape(gtfs_path, _bus_routes(gtfs_path))
    tracks: dict[tuple, set] = {}
    for shape_id, points in _simplified_shapes(gtfs_path, set(lines_by_shape)):
        if len(points) < 2:
            continue
        key = tuple(
            (round(lon, PRECISION), round(lat, PRECISION)) for lon, lat in points
        )
        tracks.setdefault(key, set()).update(lines_by_shape[shape_id])
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [list(p) for p in key]},
            "properties": {
                "lines": sorted(names),
                "search": " ".join(sorted(names)).lower(),
            },
        }
        for key, names in tracks.items()
    ]
    return {"type": "FeatureCollection", "features": features}
