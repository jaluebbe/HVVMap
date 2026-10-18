"""Distance helpers for walking along a polyline of (lon, lat) points."""

import math

Coord = tuple[float, float]  # (lon, lat)


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in meters."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def cumulative_distances(coords: list[Coord]) -> list[float]:
    """Distance from the first point to each point, in meters."""
    dists = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        dists.append(dists[-1] + haversine(lon1, lat1, lon2, lat2))
    return dists


def point_at_distance(coords: list[Coord], dists: list[float], target: float) -> Coord:
    """Point `target` meters along the polyline, clamped to its ends."""
    if target <= 0:
        return coords[0]
    if target >= dists[-1]:
        return coords[-1]
    for i in range(1, len(dists)):
        if dists[i] >= target:
            seg_len = dists[i] - dists[i - 1]
            if seg_len == 0:
                return coords[i]
            frac = (target - dists[i - 1]) / seg_len
            lon1, lat1 = coords[i - 1]
            lon2, lat2 = coords[i]
            return (lon1 + frac * (lon2 - lon1), lat1 + frac * (lat2 - lat1))
    return coords[-1]
