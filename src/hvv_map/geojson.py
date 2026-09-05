"""Build a GeoJSON FeatureCollection of current vehicle positions from a
getVehicleMap response (already filtered and track-enriched, see fetcher.py).
"""

import time

from hvv_map.lines import REPLACEMENT_BUS_MODES
from hvv_map.positions import interpolate_journey_position_detailed

ICON_HEIGHT = 10
MODE_BY_VEHICLE_TYPE = {
    "U_BAHN": "U",
    "S_BAHN": "S",
    "A_BAHN": "AKN",
    "SCHIFF": "FERRY",
}

# S1-only data quirk: some journey representations carry a stale/misleading
# segment.destination, flagged by a "REALTIME" placeholder in the journeyID.
# Doesn't apply to any other line.
S1_LINE_NAME = "S1"
S1_AIRPORT_STATION = "Hamburg Airport (Flughafen)"


def _s1_realtime_id(journey: dict) -> bool:
    return "REALTIME" in journey.get("journeyID", "")


def _s1_starts_or_ends_at_airport(journey: dict) -> bool:
    segments = journey.get("segments", [])
    if not segments:
        return False
    first_start = segments[0].get("startStationName", "")
    last_end = segments[-1].get("endStationName", "")
    return first_start == S1_AIRPORT_STATION or last_end == S1_AIRPORT_STATION


def _s1_should_keep(journey: dict, destination: str) -> bool:
    direction = journey.get("line", {}).get("direction", "")
    if _s1_starts_or_ends_at_airport(journey):
        return True
    if _s1_realtime_id(journey):
        if destination == direction:
            return True
        return direction in destination
    return direction != S1_AIRPORT_STATION


def mode_for_journey(journey: dict) -> str:
    vehicle_type = journey.get("vehicleType", "")
    if vehicle_type == "REGIONALBUS":
        return REPLACEMENT_BUS_MODES.get(journey.get("line", {}).get("id", ""), "")
    return MODE_BY_VEHICLE_TYPE.get(vehicle_type, "")


def _short_label(destination: str) -> str:
    """Drop parenthetical suffixes, e.g. 'Lattenkamp (Sporthalle)' -> 'Lattenkamp'."""
    return destination.split("(")[0].strip()


def _build_feature(
    journey: dict,
    progress: float,
    position: tuple[float, float],
    destination: str,
    delay_minutes: int,
) -> dict:
    line = journey.get("line", {})
    line_id = line.get("id", "")
    model = (line.get("type") or {}).get("model", "")
    lon, lat = position
    text = f"{destination}<br>{model}" if model else destination
    if delay_minutes > 0:
        text = f"{text}<br>+{delay_minutes} Minuten"
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "journeyID": journey.get("journeyID", ""),
            "icon": f"https://cloud.geofox.de/icon/line?height={ICON_HEIGHT}&lineKey={line_id}&fileFormat=SVG",
            "iconHeight": ICON_HEIGHT,
            "line": line.get("name", ""),
            "label": _short_label(destination),
            "text": text,
            "progress": round(progress, 3),
            "mode": mode_for_journey(journey),
            "delay": delay_minutes,
        },
    }


def build_positions_geojson(vehiclemap_data: dict, now_ts: int | None = None) -> dict:
    """vehiclemap_data: the 'data' payload of the hvv:vehiclemap Redis entry
    (journeys already filtered and track-enriched by fetcher.py).

    Uses the SEGMENT's own "destination" field for the label, not
    journey.line.direction - confirmed against real data that destination
    already contains the combined value (e.g. "Poppenbüttel / Hamburg
    Airport (Flughafen)") throughout a coupled train's shared trunk section,
    while line.direction only ever shows one half's final destination. On
    U3 (Hamburg's only ring line), this also means showing interim
    waypoints (e.g. "Hauptbahnhof Süd") rather than the nominal loop
    endpoint - intentional, since a rider boarding mid-ring usually cares
    more about the next stretch than the full-loop terminus.

    S1 additionally gets journeys dropped per _s1_should_keep() - a
    data quirk unrelated to the coupling above (see its docstring).
    """
    if now_ts is None:
        now_ts = int(time.time())

    groups: dict[tuple, list[tuple[dict, float, tuple[float, float], str, int]]] = {}
    for journey in vehiclemap_data.get("journeys", []):
        result = interpolate_journey_position_detailed(journey, now_ts)
        if result is None:
            continue
        progress, position, segment = result
        destination = segment.get("destination", "")
        line_name = journey.get("line", {}).get("name", "")
        delay_minutes = int(segment.get("realtimeDelay") or 0)

        if line_name == S1_LINE_NAME and not _s1_should_keep(journey, destination):
            continue

        key = (line_name, id(journey))
        groups.setdefault(key, []).append(
            (journey, progress, position, destination, delay_minutes)
        )

    features = []
    for entries in groups.values():
        journey, progress, position, destination, delay_minutes = entries[0]
        features.append(
            _build_feature(journey, progress, position, destination, delay_minutes)
        )

    return {"type": "FeatureCollection", "features": features}
