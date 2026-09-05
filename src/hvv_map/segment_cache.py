"""Self-populating segment geometry cache.

getVehicleMap is queried with withoutCoords=True (see fetcher.py) - segment
geometry is looked up here instead. Cache misses are fetched via
getTrackCoordinates (batched: multiple missing segments in one call) and
written back, so repeat lookups are free.
"""

import json
import os
import sqlite3
import time

from hvv_map.gti_client import GtiClient

DEFAULT_DB_PATH = os.environ.get("SEGMENT_CACHE_PATH", "segment_cache.db")
# How long a segment/line pairing can go unobserved before it's excluded
# from the reference lines layer. Kept generous (days, not minutes) - lines
# genuinely pause overnight and some ferries are operated on the weekend only,
# so a short threshold would wrongly hide them during normal service gaps.
STALE_THRESHOLD_SECONDS = 6 * 24 * 60 * 60

Track = list[float]  # flat [lon, lat, lon, lat, ...]
SegmentKey = tuple[str, str]  # (start_stop_point_key, end_stop_point_key)


def open_cache(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS segment_cache ("
        "start_key TEXT, end_key TEXT, track_json TEXT, "
        "PRIMARY KEY (start_key, end_key))"
    )
    # Which line(s)/mode(s) have been observed using a segment, and when
    # each was last seen - grows organically as fetch_vehicle_map()
    # processes real journeys. last_seen lets a stale pairing (line no
    # longer running there - closure, or a SEV that has since ended) age
    # out of the reference lines layer; see STALE_THRESHOLD_SECONDS and
    # all_segments_with_lines().
    conn.execute(
        "CREATE TABLE IF NOT EXISTS segment_lines ("
        "start_key TEXT, end_key TEXT, line_name TEXT, mode TEXT, last_seen INTEGER, "
        "PRIMARY KEY (start_key, end_key, line_name))"
    )
    # Same idea as segment_lines, but keyed by STATION NAME rather than
    # stopPointKey - stops (built from listLines/listStations, a different
    # ID namespace than segment_cache's stopPointKeys) can only be
    # cross-checked for freshness via the name they share with
    # startStationName/endStationName on an observed segment. Matching by
    # name is approximate (naming can differ slightly between endpoints,
    # e.g. "Lattenkamp" vs "Lattenkamp (Sporthalle)"), but no
    # shared ID exists to match on instead.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS station_activity ("
        "station_name TEXT PRIMARY KEY, last_seen INTEGER)"
    )
    return conn


def record_station_seen(
    conn: sqlite3.Connection, station_name: str, seen_at: int | None = None
) -> None:
    if not station_name:
        return
    if seen_at is None:
        seen_at = int(time.time())
    conn.execute(
        "INSERT INTO station_activity (station_name, last_seen) VALUES (?, ?) "
        "ON CONFLICT(station_name) DO UPDATE SET last_seen=excluded.last_seen",
        (station_name, seen_at),
    )
    conn.commit()


def active_station_names(
    conn: sqlite3.Connection, stale_after_seconds: int = STALE_THRESHOLD_SECONDS
) -> set[str]:
    """Station names seen on at least one segment within stale_after_seconds."""
    cutoff = int(time.time()) - stale_after_seconds
    rows = conn.execute(
        "SELECT station_name FROM station_activity WHERE last_seen >= ?", (cutoff,)
    ).fetchall()
    return {row[0] for row in rows}


def record_line_usage(
    conn: sqlite3.Connection,
    key: SegmentKey,
    line_name: str,
    mode: str,
    seen_at: int | None = None,
) -> None:
    if not line_name:
        return
    if seen_at is None:
        seen_at = int(time.time())
    start_key, end_key = key
    conn.execute(
        "INSERT INTO segment_lines (start_key, end_key, line_name, mode, last_seen) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(start_key, end_key, line_name) "
        "DO UPDATE SET last_seen=excluded.last_seen",
        (start_key, end_key, line_name, mode, seen_at),
    )
    conn.commit()


def get_lines_for_segment(
    conn: sqlite3.Connection, key: SegmentKey, min_last_seen: int | None = None
) -> list[tuple[str, str]]:
    """Returns [(line_name, mode), ...] observed on this segment. With
    min_last_seen, only pairings seen at or after that timestamp count -
    stale ones (see STALE_THRESHOLD_SECONDS) are left out."""
    start_key, end_key = key
    if min_last_seen is None:
        rows = conn.execute(
            "SELECT line_name, mode FROM segment_lines "
            "WHERE start_key = ? AND end_key = ?",
            (start_key, end_key),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT line_name, mode FROM segment_lines "
            "WHERE start_key = ? AND end_key = ? AND last_seen >= ?",
            (start_key, end_key, min_last_seen),
        ).fetchall()
    return list(rows)


def all_segments_with_lines(
    conn: sqlite3.Connection, stale_after_seconds: int = STALE_THRESHOLD_SECONDS
) -> list[tuple[SegmentKey, Track, list[tuple[str, str]]]]:
    """All cached segments with at least one recently-observed line, with
    their geometry and the (line_name, mode) pairs seen on them - the raw
    material for building a reference lines GeoJSON. A segment whose only
    line pairing(s) haven't been seen within stale_after_seconds is left
    out entirely - e.g. a SEV that has since ended."""
    cutoff = int(time.time()) - stale_after_seconds
    rows = conn.execute(
        "SELECT sc.start_key, sc.end_key, sc.track_json FROM segment_cache sc "
        "WHERE EXISTS ("
        "  SELECT 1 FROM segment_lines sl "
        "  WHERE sl.start_key = sc.start_key AND sl.end_key = sc.end_key "
        "  AND sl.last_seen >= ?"
        ")",
        (cutoff,),
    ).fetchall()
    result = []
    for start_key, end_key, track_json in rows:
        key = (start_key, end_key)
        lines = get_lines_for_segment(conn, key, min_last_seen=cutoff)
        if not lines:
            continue
        result.append((key, json.loads(track_json), lines))
    return result


def get_cached(conn: sqlite3.Connection, key: SegmentKey) -> Track | None:
    start_key, end_key = key
    row = conn.execute(
        "SELECT track_json FROM segment_cache WHERE start_key = ? AND end_key = ?",
        (start_key, end_key),
    ).fetchone()
    return json.loads(row[0]) if row else None


def store(conn: sqlite3.Connection, key: SegmentKey, track: Track) -> None:
    start_key, end_key = key
    conn.execute(
        "INSERT OR REPLACE INTO segment_cache (start_key, end_key, track_json) "
        "VALUES (?, ?, ?)",
        (start_key, end_key, json.dumps(track)),
    )
    conn.commit()


def _fetch_from_api(
    client: GtiClient, keys: list[SegmentKey]
) -> dict[SegmentKey, Track]:
    """One batched getTrackCoordinates call for all given segments."""
    stop_point_keys = [
        k for pair in keys for k in pair
    ]  # interleave start,end,start,end,...
    request = {
        "version": 63,
        "coordinateType": "EPSG_4326",
        "stopPointKeys": stop_point_keys,
    }
    response = client.send("getTrackCoordinates", request)
    result = {}
    for track_id, track_data in zip(
        response.get("trackIDs", []), response.get("tracks", [])
    ):
        start_key, end_key = track_id.split("*", 1)
        result[(start_key, end_key)] = track_data.get("track", [])
    return result


def get_or_fetch_tracks(
    conn: sqlite3.Connection, client: GtiClient, keys: list[SegmentKey]
) -> dict[SegmentKey, Track]:
    """Look up each segment in the cache; batch-fetch and store whatever's missing."""
    result: dict[SegmentKey, Track] = {}
    missing: list[SegmentKey] = []
    for key in keys:
        cached = get_cached(conn, key)
        if cached is not None:
            result[key] = cached
        else:
            missing.append(key)

    if missing:
        fetched = _fetch_from_api(client, missing)
        for key, track in fetched.items():
            store(conn, key, track)
            result[key] = track

    return result
