"""Group the cached departureList data into trees for the departures debug
page: mode -> line -> station -> departure, and mode -> line -> trip -> stop.

Pure functions over the Redis payloads written by departures.py
(hvv:departures:merged and hvv:departures:by_service), so the api image
needs no GTI client.
"""

import re
from datetime import datetime, timedelta

from hvv_map.gtilive_geojson import mode_for_line

OTHER_MODE = "OTHER"  # lines mode_for_line() cannot map
MODE_ORDER = ["U", "S", "AKN", "R", "FERRY", OTHER_MODE]


def parse_anchor(data: dict | None) -> datetime | None:
    """The query anchor ({"date": "YYYY-MM-DD", "time": "HH:MM:SS"}) as a naive
    local datetime, None if missing or malformed."""
    try:
        return datetime.strptime(f"{data['date']} {data['time']}", "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return None


def _clock(anchor: datetime | None, offset) -> tuple[str | None, bool]:
    """("HH:MM", whether it falls on the day after the anchor's) for a
    timeOffset in minutes; (None, False) without anchor or offset."""
    if anchor is None or offset is None:
        return None, False
    moment = anchor + timedelta(minutes=offset)
    return moment.strftime("%H:%M"), moment.date() != anchor.date()


def _natural_key(text: str) -> list:
    """Sort key putting "U2" before "U10"."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def _station_name(station: dict) -> str:
    return (
        station.get("combinedName") or station.get("name") or station.get("id") or "?"
    )


# Hamburg U-Bahn fleet: (first, last number, series). GTI's line.type.model
# is the planned type and can be wrong, the vehicle numbers are authoritative.
UBAHN_SERIES = [
    (101, 130, "DT4.1"),
    (131, 151, "DT4.2"),
    (152, 171, "DT4.3"),
    (172, 186, "DT4.4"),
    (187, 211, "DT4.5"),
    (212, 226, "DT4.6"),
    (301, 418, "DT5.1"),
    (419, 463, "DT5.2"),
]


def ubahn_series(number) -> str | None:
    """Series ("DT4.3", "DT5.1", ...) of a U-Bahn unit number, None if unknown."""
    try:
        value = int(number)
    except (TypeError, ValueError):
        return None
    for first, last, series in UBAHN_SERIES:
        if first <= value <= last:
            return series
    return None


def vehicles_text(vehicles, ubahn: bool = False) -> str:
    """ "4711 [DT5.1], ..." for a departure's vehicles list, "" if none; the
    series is added for U-Bahn units, the id only when there is no number."""
    parts = []
    for vehicle in vehicles or []:
        number, vehicle_id = vehicle.get("number"), vehicle.get("id")
        series = ubahn_series(number) if ubahn else None
        text = " ".join(
            filter(
                None,
                [
                    number and str(number),
                    series and f"[{series}]",
                    not number and vehicle_id and f"(id {vehicle_id})",
                ],
            )
        )
        if text:
            parts.append(text)
    return ", ".join(parts)


def _is_ubahn(line: dict) -> bool:
    return (line.get("type") or {}).get("shortInfo") == "U"


def refined_model(line: dict, vehicles) -> str | None:
    """line.type.model, replaced by the type derived from the U-Bahn vehicle
    numbers (DT4/DT5, "DT4/DT5" if mixed) when they are known."""
    model = (line.get("type") or {}).get("model")
    if not _is_ubahn(line):
        return model
    types = {
        s[:3] for s in map(ubahn_series, (v.get("number") for v in vehicles or [])) if s
    }
    return "/".join(sorted(types)) if types else model


def _mode_of(line: dict) -> str:
    return mode_for_line(line) or OTHER_MODE


def _mode_sort_key(mode: str) -> int:
    return MODE_ORDER.index(mode) if mode in MODE_ORDER else len(MODE_ORDER)


def build_station_tree(departures: list[dict], anchor: datetime | None) -> dict:
    """mode -> line -> station -> departures, every level with its counts."""
    modes: dict[str, dict] = {}
    for dep in departures:
        line = dep.get("line") or {}
        station = dep.get("station") or {}
        lines = modes.setdefault(_mode_of(line), {})
        entry = lines.setdefault(
            (line.get("name", ""), line.get("id", "")), {"stations": {}}
        )
        station_id = station.get("id") or _station_name(station)
        stop = entry["stations"].setdefault(
            station_id,
            {"id": station_id, "name": _station_name(station), "departures": []},
        )
        offset = dep.get("timeOffset")
        time_text, next_day = _clock(anchor, offset)
        stop["departures"].append(
            {
                "offset": offset,
                "time": time_text,
                "next_day": next_day,
                "direction": line.get("direction"),
                "service_id": dep.get("serviceId"),
                "direction_id": dep.get("directionId"),
                "platform": dep.get("realtimePlatform") or dep.get("platform"),
                "delay": dep.get("delay"),
                "vehicles": vehicles_text(dep.get("vehicles"), _is_ubahn(line)),
                "model": refined_model(line, dep.get("vehicles")),
            }
        )

    result = []
    for mode in sorted(modes, key=_mode_sort_key):
        line_nodes = []
        for (name, line_id), entry in sorted(
            modes[mode].items(), key=lambda kv: _natural_key(kv[0][0])
        ):
            stations = sorted(entry["stations"].values(), key=lambda s: s["name"])
            for station in stations:
                station["departures"].sort(key=lambda d: d["offset"] or 0)
                station["departure_count"] = len(station["departures"])
            line_nodes.append(
                {
                    "name": name,
                    "id": line_id,
                    "station_count": len(stations),
                    "departure_count": sum(s["departure_count"] for s in stations),
                    "stations": stations,
                }
            )
        result.append(
            {
                "mode": mode,
                "line_count": len(line_nodes),
                "departure_count": sum(n["departure_count"] for n in line_nodes),
                "lines": line_nodes,
            }
        )
    return {"modes": result}


def build_trip_tree(by_service: dict, anchor: datetime | None) -> dict:
    """mode -> line -> trip (one serviceId) -> stops. Each trip says whether
    departures.match_sublines resolved it to a subline."""
    modes: dict[str, dict] = {}
    for service_id, entry in by_service.items():
        line = entry.get("line") or {}
        stops = []
        for stop in entry.get("stops", []):
            time_text, next_day = _clock(anchor, stop.get("timeOffset"))
            stops.append(
                {
                    "offset": stop.get("timeOffset"),
                    "time": time_text,
                    "next_day": next_day,
                    "name": _station_name(stop.get("station") or {}),
                    "platform": stop.get("realtimePlatform") or stop.get("platform"),
                    "vehicles": vehicles_text(stop.get("vehicles"), _is_ubahn(line)),
                }
            )
        subline = entry.get("subline")
        trip = {
            "service_id": service_id,
            "direction_id": entry.get("directionId"),
            "origin": line.get("origin"),
            "destination": entry.get("destination"),
            "model": refined_model(
                line,
                next(
                    (
                        st["vehicles"]
                        for st in entry.get("stops", [])
                        if st.get("vehicles")
                    ),
                    None,
                ),
            ),
            "stop_count": len(stops),
            "subline_stop_count": len(subline) if subline else None,
            "stops": stops,
        }
        lines = modes.setdefault(_mode_of(line), {})
        node = lines.setdefault(
            (line.get("name", ""), line.get("id", "")), {"trips": []}
        )
        node["trips"].append(trip)

    result = []
    for mode in sorted(modes, key=_mode_sort_key):
        line_nodes = []
        for (name, line_id), node in sorted(
            modes[mode].items(), key=lambda kv: _natural_key(kv[0][0])
        ):
            trips = sorted(
                node["trips"],
                key=lambda t: (t["stops"][0]["offset"] or 0) if t["stops"] else 0,
            )
            line_nodes.append(
                {
                    "name": name,
                    "id": line_id,
                    "trip_count": len(trips),
                    "resolved_count": sum(
                        1 for t in trips if t["subline_stop_count"] is not None
                    ),
                    "trips": trips,
                }
            )
        result.append(
            {
                "mode": mode,
                "line_count": len(line_nodes),
                "trip_count": sum(n["trip_count"] for n in line_nodes),
                "resolved_count": sum(n["resolved_count"] for n in line_nodes),
                "lines": line_nodes,
            }
        )
    return {"modes": result}
