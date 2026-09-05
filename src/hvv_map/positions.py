"""Interpolate a vehicle's current position along its current segment track.

startDateTime/endDateTime already include realtime delay - no separate
delay correction needed here.
"""

import math

Coord = tuple[float, float]  # (lon, lat)


def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
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


def _track_to_coords(flat_track: list[float]) -> list[Coord]:
    """[lon, lat, lon, lat, ...] -> [(lon, lat), ...]."""
    return list(zip(flat_track[0::2], flat_track[1::2]))


def _cumulative_distances(coords: list[Coord]) -> list[float]:
    dists = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        dists.append(dists[-1] + _haversine(lon1, lat1, lon2, lat2))
    return dists


def _point_at_distance(coords: list[Coord], dists: list[float], target: float) -> Coord:
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


def interpolate_position(segment: dict, now_ts: int) -> tuple[float, Coord] | None:
    """Position along ONE segment's track at now_ts. None if no usable track."""
    track_field = segment.get("track")
    if not track_field:
        return None
    coords = _track_to_coords(track_field.get("track", []))
    if len(coords) < 2:
        return None

    start = int(segment["startDateTime"])
    end = int(segment["endDateTime"])
    if end <= start:
        progress = 1.0
    else:
        progress = (now_ts - start) / (end - start)
        progress = max(0.0, min(1.0, progress))

    dists = _cumulative_distances(coords)
    target_dist = progress * dists[-1]
    return progress, _point_at_distance(coords, dists, target_dist)


def _select_segment(journey: dict, now_ts: int) -> dict | None:
    """Pick the segment covering now_ts; fall back to nearest by time."""
    segments = journey.get("segments", [])
    if not segments:
        return None
    for segment in segments:
        if int(segment["startDateTime"]) <= now_ts <= int(segment["endDateTime"]):
            return segment
    return min(segments, key=lambda s: abs(int(s["startDateTime"]) - now_ts))


def interpolate_journey_position_detailed(
    journey: dict, now_ts: int
) -> tuple[float, Coord, dict] | None:
    """Position of a full journey (as returned by getVehicleMap) at now_ts,
    plus the segment used to get there - callers need it for fields like
    destination or realtimeDelay that live on the segment, not the
    position/progress alone."""
    segment = _select_segment(journey, now_ts)
    if segment is None:
        return None
    result = interpolate_position(segment, now_ts)
    if result is None:
        return None
    progress, position = result
    return progress, position, segment
