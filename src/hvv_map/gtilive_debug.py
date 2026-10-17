"""Debug GeoJSON for the gti-live layers: the same lines/stops that
gtilive_geojson draws, plus the properties needed to see why a line looks
the way it does - how many services ride it, how much of it has real track
geometry, and which services found no subline at all.
"""

import sqlite3

from hvv_map.gti_shapes import subline_key
from hvv_map.gtilive_geojson import _hop_runs, _subline_coordinates, mode_for_line
from hvv_map.lines import LINE_COLORS
from hvv_map.stations import StationInfo

UNKNOWN_COLOR = "888888"  # same fallback as gtilive_geojson


def _station_name(station_id: str, fallback: str, stations: dict) -> str:
    station = stations.get(station_id)
    return station.combined_name if station else fallback


def _line_features(key, info, stations, shapes_conn) -> list[dict]:
    """One feature per run of real/straight hops, so the page can style
    exactly the hops that lack a stored segment. Every run carries the
    whole subline's properties."""
    line_id, station_ids = key
    names = sorted(info["line_names"])
    color = LINE_COLORS.get(names[0])
    runs = _hop_runs(station_ids, shapes_conn)
    stop_names = [_station_name(i, n, stations) for i, n in info["stops"]]
    properties = {
        "kind": "line",
        "lines": names,
        "mode": sorted(info["modes"])[0],
        "color": f"#{color or UNKNOWN_COLOR}",
        "color_known": color is not None,
        "services": info["services"],
        "hops": len(station_ids) - 1,
        "real_hops": sum(len(ids) - 1 for real, ids in runs if real),
        "stops": stop_names,
        "line_id": line_id,
        "subline_key": subline_key(line_id, station_ids),
        "search": " ".join([*names, *stop_names]).lower(),
    }
    features = []
    for real, ids in runs:
        coordinates = _subline_coordinates(ids, stations, shapes_conn)
        if len(coordinates) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "properties": {**properties, "straight": not real},
            }
        )
    return features


def _stop_feature(station: StationInfo, info: dict, resolved: bool) -> dict:
    lines = sorted(info["lines"])
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [station.lon, station.lat]},
        "properties": {
            "kind": "stop",
            "id": station.id,
            "name": station.combined_name,
            "mode": sorted(info["modes"])[0],
            "modes": sorted(info["modes"]),
            "lines": lines,
            "services": info["services"],
            # False: seen in departures, but on no resolved subline.
            "resolved": resolved,
            "search": " ".join([station.combined_name, *lines]).lower(),
        },
    }


def build_debug_geojson(
    by_service: dict,
    stations: dict[str, StationInfo],
    shapes_conn: sqlite3.Connection | None = None,
) -> dict:
    """One FeatureCollection with kind="line" (one per distinct resolved
    subline) and kind="stop" features (stations on resolved sublines, plus
    "orphan" stations only seen in services without a subline), and a
    top-level "debug" summary."""
    sublines: dict[tuple, dict] = {}
    resolved_stops: dict[str, dict] = {}
    orphan_stops: dict[str, dict] = {}
    unresolved_by_line: dict[tuple, int] = {}
    total = resolved_count = 0

    names: dict[str, str] = {}  # station id -> a name seen for it

    def add_stop(bucket: dict, station_id: str, name: str, mode: str, line_name: str):
        names.setdefault(station_id, name)
        info = bucket.setdefault(
            station_id, {"modes": set(), "lines": set(), "services": 0}
        )
        info["modes"].add(mode)
        info["lines"].add(line_name)
        info["services"] += 1

    for entry in by_service.values():
        line = entry.get("line") or {}
        mode = mode_for_line(line)
        if not mode:
            continue
        total += 1
        line_name = line.get("name", "")
        subline = entry.get("subline")
        if subline:
            resolved_count += 1
            station_ids = tuple(stop["id"] for stop in subline)
            info = sublines.setdefault(
                (line.get("id", ""), station_ids),
                {
                    "line_names": set(),
                    "modes": set(),
                    "services": 0,
                    "stops": [(s["id"], s["name"]) for s in subline],
                },
            )
            info["line_names"].add(line_name)
            info["modes"].add(mode)
            info["services"] += 1
            for stop in subline:
                add_stop(resolved_stops, stop["id"], stop["name"], mode, line_name)
        else:
            key = (line_name, mode)
            unresolved_by_line[key] = unresolved_by_line.get(key, 0) + 1
            seen = set()
            for stop in entry.get("stops", []):
                station = stop.get("station") or {}
                station_id = station.get("id")
                if station_id and station_id not in seen:
                    seen.add(station_id)
                    add_stop(
                        orphan_stops,
                        station_id,
                        station.get("combinedName") or "",
                        mode,
                        line_name,
                    )

    features = []
    for key, info in sublines.items():
        features.extend(_line_features(key, info, stations, shapes_conn))

    missing_stations = []
    for bucket, resolved in ((resolved_stops, True), (orphan_stops, False)):
        for station_id, info in bucket.items():
            if not resolved and station_id in resolved_stops:
                continue
            station = stations.get(station_id)
            if station is None:
                missing_stations.append(
                    {
                        "id": station_id,
                        "name": names.get(station_id) or station_id,
                        "lines": sorted(info["lines"]),
                    }
                )
                continue
            features.append(_stop_feature(station, info, resolved))

    return {
        "type": "FeatureCollection",
        "features": features,
        "debug": {
            "services": total,
            "resolved": resolved_count,
            "unresolved": total - resolved_count,
            "unresolved_by_line": [
                {"line": name, "mode": mode, "count": count}
                for (name, mode), count in sorted(
                    unresolved_by_line.items(), key=lambda kv: (-kv[1], kv[0])
                )
            ],
            "missing_stations": missing_stations,
        },
    }
