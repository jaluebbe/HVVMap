"""Build the reference stops/lines GeoJSON layers from live API data.

Stops: from listLines(withSublines=True).stationSequence, resolved to
coordinates via stations.py (listStations). Lines: from segment_cache's
organically-grown geometry + line/mode tracking (see fetcher.py's
_inject_tracks) - a section nobody has driven through yet (e.g. a closure)
simply has no entry.

Refreshed periodically by the fetcher, could also be run standalone if needed.
"""

import json
import sqlite3
import time

from hvv_map.geojson import MODE_BY_VEHICLE_TYPE
from hvv_map.gti_client import GtiClient
from hvv_map.lines import (
    LINE_COLORS,
    REPLACEMENT_BUS_MODES,
    SublineInfo,
    fetch_sublines,
)
from hvv_map.redis_client import get_redis_client
from hvv_map.segment_cache import (
    active_station_names,
    all_segments_with_lines,
    open_cache,
)
from hvv_map.stations import StationInfo, by_id, fetch_stations

STOPS_REDIS_KEY = "hvv:reference_stops"
LINES_REDIS_KEY = "hvv:reference_lines"
REDIS_TTL = 24 * 60 * 60  # seconds; rebuilt periodically anyway, generous headroom


def mode_for_subline(subline: SublineInfo) -> str:
    if subline.vehicle_type == "REGIONALBUS":
        return REPLACEMENT_BUS_MODES.get(subline.line_id, "")
    return MODE_BY_VEHICLE_TYPE.get(subline.vehicle_type, "")


def build_stops_geojson(
    sublines: list[SublineInfo],
    stations: dict[str, StationInfo],
    active_names: set[str] | None = None,
) -> dict:
    """One Point feature per station actually served by a line of interest,
    with the set of modes observed there (e.g. a U/S interchange).

    active_names, if given, additionally requires the station's own name to
    have been recently observed on a real segment (see
    segment_cache.active_station_names) - without this, a station whose
    line has gone stale (SEV that has ended, etc.) would keep showing on
    its own, disconnected from any line geometry ("stops lying around" -
    see chat). Name-matched, not ID-matched: stationSequence's station IDs
    and segment_cache's stopPointKeys are different ID namespaces with no
    shared key, so the name is the only thing to cross-reference on -
    approximate, since naming can differ slightly between endpoints.
    """
    modes_by_station: dict[str, set[str]] = {}
    for subline in sublines:
        mode = mode_for_subline(subline)
        if not mode:
            continue
        for station_id in subline.station_ids:
            modes_by_station.setdefault(station_id, set()).add(mode)

    features = []
    for station_id, modes in modes_by_station.items():
        station = stations.get(station_id)
        if station is None:
            continue
        if active_names is not None and station.name not in active_names:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [station.lon, station.lat],
                },
                "properties": {
                    "id": station.id,
                    "name": station.name,
                    "modes": sorted(modes),
                    "text": station.name,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def build_lines_geojson(conn: sqlite3.Connection) -> dict:
    """One LineString feature per cached segment that has at least one
    observed line, with the line names and modes seen on it. Color comes
    from the first (alphabetically) line name observed there - segments
    shared by multiple lines (e.g. U1 + U1-ERSATZ) get a single, stable
    color rather than one per line."""
    features = []
    for _key, track, lines in all_segments_with_lines(conn):
        coordinates = list(zip(track[0::2], track[1::2]))
        if len(coordinates) < 2:
            continue
        line_names = sorted({name for name, _mode in lines})
        modes = sorted({mode for _name, mode in lines if mode})
        color = LINE_COLORS.get(line_names[0], "888888") if line_names else "888888"
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [list(c) for c in coordinates],
                },
                "properties": {
                    "lines": line_names,
                    "modes": modes,
                    "color": f"#{color}",
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _store(redis_client, key: str, geojson: dict) -> None:
    payload = json.dumps({"fetched_at": int(time.time()), "data": geojson})
    redis_client.set(key, payload, ex=REDIS_TTL)


def finish_reference_rebuild(
    client: GtiClient,
    redis_client,
    conn: sqlite3.Connection,
    sublines: list[SublineInfo],
) -> tuple[int, int]:
    """Second half of build_and_store_reference - takes sublines already
    fetched (see fetch_sublines) and does the remaining API call
    (listStations) plus building/storing both layers. Split out so the two
    API calls can land in separate fetcher cycles instead of back-to-back
    (see chat: keeps the continuous loop within its 1 req/s budget even
    during a rebuild)."""
    stations = by_id(fetch_stations(client))
    active_names = active_station_names(conn)
    stops_geojson = build_stops_geojson(sublines, stations, active_names)
    _store(redis_client, STOPS_REDIS_KEY, stops_geojson)

    lines_geojson = build_lines_geojson(conn)
    _store(redis_client, LINES_REDIS_KEY, lines_geojson)

    return len(stops_geojson["features"]), len(lines_geojson["features"])


def build_and_store_reference(
    client: GtiClient, redis_client, conn: sqlite3.Connection
) -> tuple[int, int]:
    """(Re)build both reference layers and store them in Redis in one call -
    fine for one-off CLI use (main()), where the two API calls landing in
    the same second doesn't matter. The continuous fetcher instead calls
    fetch_sublines() and finish_reference_rebuild() separately, a cycle
    apart - see fetcher.py."""
    sublines = fetch_sublines(client)
    return finish_reference_rebuild(client, redis_client, conn, sublines)


def main() -> None:
    """CLI: (re)build both reference layers and store them in Redis."""
    stop_count, line_count = build_and_store_reference(
        GtiClient(), get_redis_client(), open_cache()
    )
    print(f"{stop_count} stops stored ({STOPS_REDIS_KEY})")
    print(f"{line_count} line segments stored ({LINES_REDIS_KEY})")


if __name__ == "__main__":
    main()
