"""Build a GeoJSON of affected stations from announcements, using the
category/validity classification from announcement_categories.py.

Station resolution uses stations.py (listStations) - only
explicitly named begin/end stations are marked, no intermediate-stop
expansion (could be added later via listLines(withSublines=True).stationSequence).
"""

from datetime import datetime, timezone

from hvv_map.announcement_categories import (
    CATEGORY_COLORS,
    classify_category,
    is_currently_valid,
)
from hvv_map.lines import FERRY_CARRIER
from hvv_map.stations import StationInfo

MARKER_RADIUS = 5

BASE_LINE_MODE = {
    "U1": "U", "U2": "U", "U3": "U", "U4": "U",
    "S1": "S", "S2": "S", "S3": "S", "S4": "S", "S5": "S", "S6": "S", "S7": "S",
    "A1": "AKN", "A2": "AKN", "A3": "AKN",
}


def _location_station_ids(location: dict) -> list[str]:
    ids = []
    for key in ("begin", "end"):
        sd_name = location.get(key)
        if sd_name and sd_name.get("id"):
            ids.append(sd_name["id"])
    return ids


def _location_mode(location: dict) -> str:
    line = location.get("line")
    if line:
        return BASE_LINE_MODE.get(line.get("name", ""), "")
    if (
        location.get("name") == FERRY_CARRIER
    ):  # operator-level location (all HADAG lines)
        return "FERRY"
    return ""


def _strip_redundant_prefix(summary: str, station_name: str) -> str:
    """Some summaries already start with 'StationName: ...' - avoid it
    appearing twice once we prepend our own station-name heading."""
    prefix = f"{station_name}:"
    if summary.lower().startswith(prefix.lower()):
        return summary[len(prefix) :].strip()
    return summary


def build_disruptions_geojson(
    announcements_data: dict,
    stations_by_id: dict[str, StationInfo],
    now: datetime | None = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)

    # (station_id, category) -> {"summaries": [...], "modes": set()}
    grouped: dict[tuple[str, str], dict] = {}
    for announcement in announcements_data.get("announcements", []):
        if not is_currently_valid(announcement, now):
            continue
        category = classify_category(announcement)
        summary = announcement.get("summary") or ""
        for location in announcement.get("locations", []):
            mode = _location_mode(location)
            for station_id in _location_station_ids(location):
                entry = grouped.setdefault(
                    (station_id, category), {"summaries": [], "modes": set()}
                )
                if summary and summary not in entry["summaries"]:
                    entry["summaries"].append(summary)
                if mode:
                    entry["modes"].add(mode)

    features = []
    for (station_id, category), entry in grouped.items():
        station = stations_by_id.get(station_id)
        if station is None:
            continue
        cleaned = [_strip_redundant_prefix(s, station.name) for s in entry["summaries"]]
        text = f"{station.name}:<br>" + "<br><br>".join(cleaned)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [station.lon, station.lat],
                },
                "properties": {
                    "color": CATEGORY_COLORS[category],
                    "fill": True,
                    "markerRadius": MARKER_RADIUS,
                    "text": text,
                    "station_name": station.name,
                    "category": category,
                    "modes": sorted(entry["modes"]),
                    "message_count": len(cleaned),
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}
