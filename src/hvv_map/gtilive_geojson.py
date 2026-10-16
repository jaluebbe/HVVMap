"""Build the gti-live stops/lines GeoJSON layers straight from departureList
data (see departures.py) - a proof of concept for an anchor-departures +
sublines based alternative to the segment_cache/getVehicleMap-driven
reference layers, with no dependency on getVehicleMap at all.
"""

import sqlite3

from hvv_map.gti_shapes import subline_hops
from hvv_map.lines import LINE_COLORS, replacement_bus_mode
from hvv_map.stations import StationInfo

# departureList's line.type.shortInfo values, mapped to our map mode keys.
_SHORT_INFO_MODE = {
    "U": "U",
    "S": "S",
    "A": "AKN",
    "Schiff": "FERRY",
    "RB": "R",
    "RE": "R",
}


def mode_for_line(line: dict) -> str:
    """Map a departureList line to our map mode key. Replacement buses
    (shortInfo "Bus") are resolved by name instead, like elsewhere."""
    short_info = (line.get("type") or {}).get("shortInfo", "")
    if short_info == "Bus":
        return replacement_bus_mode(line.get("name", ""))
    return _SHORT_INFO_MODE.get(short_info, "")


def build_stops_geojson(by_service: dict, stations: dict[str, StationInfo]) -> dict:
    """One Point feature per station on a resolved subline match (see
    departures.match_sublines), with the set of modes seen there."""
    modes_by_station: dict[str, set[str]] = {}
    for entry in by_service.values():
        subline = entry.get("subline")
        mode = mode_for_line(entry["line"])
        if not subline or not mode:
            continue
        for stop in subline:
            modes_by_station.setdefault(stop["id"], set()).add(mode)

    features = []
    for station_id, modes in modes_by_station.items():
        station = stations.get(station_id)
        if station is None:
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
                    # combined_name, not name: listStations sometimes splits
                    # a station's full name into name/city (e.g. name="Hbf",
                    # city="Lübeck") - combined_name is the recombined,
                    # human-readable form ("Lübeck Hbf").
                    "name": station.combined_name,
                    "modes": sorted(modes),
                    "text": station.combined_name,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _subline_coordinates(
    station_ids: tuple[str, ...],
    stations: dict[str, StationInfo],
    shapes_conn: sqlite3.Connection | None,
) -> list[list[float]]:
    """Real per-segment track geometry from gti_shapes.py where a segment
    has been resolved via departureCourse, straight station-to-station line
    otherwise (or everywhere, if shapes_conn is None - no resolved data
    available yet). Built hop by hop rather than needing every station's own
    coordinate upfront: a resolved segment carries its own coordinates, so a
    declared station missing from hvv:stations (e.g. a retired platform id
    listLines' stationSequence still references, but listStations no longer
    does) only drops that one hop, not the whole line - see
    build_lines_geojson."""
    coordinates: list[list[float]] = []
    for from_id, to_id, segment in subline_hops(shapes_conn, station_ids):
        if segment is None:
            from_station = stations.get(from_id)
            to_station = stations.get(to_id)
            if from_station is None or to_station is None:
                continue  # no resolved segment and no coordinate either - skip this hop
            segment = [
                [from_station.lon, from_station.lat],
                [to_station.lon, to_station.lat],
            ]
        if coordinates and segment[0] == coordinates[-1]:
            coordinates.extend(segment[1:])
        else:
            coordinates.extend(segment)
    return coordinates


def build_lines_geojson(
    by_service: dict,
    stations: dict[str, StationInfo],
    shapes_conn: sqlite3.Connection | None = None,
) -> dict:
    """One LineString per distinct resolved subline (station-id sequence),
    deduplicated across repeat trips on the same route. Draws each segment's
    real track wherever gti_shapes.py has resolved it via departureCourse,
    straight lines elsewhere - see _subline_coordinates(). Skipped only if
    that leaves fewer than two usable points - a single missing station
    doesn't drop the whole line, since a resolved segment carries its own
    coordinates regardless of hvv:stations."""
    by_station_ids: dict[tuple, dict] = {}
    for entry in by_service.values():
        subline = entry.get("subline")
        mode = mode_for_line(entry["line"])
        if not subline or len(subline) < 2 or not mode:
            continue
        line_id = entry["line"].get("id", "")
        station_ids = tuple(stop["id"] for stop in subline)
        key = (line_id, station_ids)
        info = by_station_ids.setdefault(key, {"line_names": set(), "modes": set()})
        info["line_names"].add(entry["line"].get("name", ""))
        info["modes"].add(mode)

    features = []
    for (_line_id, station_ids), info in by_station_ids.items():
        coordinates = _subline_coordinates(station_ids, stations, shapes_conn)
        if len(coordinates) < 2:
            continue
        line_names = sorted(info["line_names"])
        color = LINE_COLORS.get(line_names[0], "888888")
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "properties": {
                    "lines": line_names,
                    "modes": sorted(info["modes"]),
                    "color": f"#{color}",
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
