"""Categorize announcements and build a GeoJSON of affected stations.

Station resolution uses stations.py (listStations) - only
explicitly named begin/end stations are marked, no intermediate-stop
expansion (could be added later via listLines(withSublines=True).stationSequence).
"""

import re
from datetime import datetime, timezone

from hvv_map.lines import FERRY_CARRIER
from hvv_map.stations import StationInfo

CATEGORY_SPERRUNG = "SPERRUNG"
CATEGORY_BARRIEREFREIHEIT = "BARRIEREFREIHEIT"
CATEGORY_SONSTIGE = "SONSTIGE"
CATEGORY_COLORS = {
    CATEGORY_SPERRUNG: "#ff6600",
    CATEGORY_BARRIEREFREIHEIT: "#42A5F5",
    CATEGORY_SONSTIGE: "#888888",
}
MARKER_RADIUS = 5

# Reference to the "Rollstuhl/Kinderwagen" search option in the hvv journey
# planner, or an elevator explicitly reported out of service - both signal
# an accessibility notice rather than a service disruption. Spelling varies
# (with/without spaces, with/without quotes), hence the regex.
ACCESSIBILITY_PATTERNS = [
    re.compile(r"Rollstuhl\s*/\s*Kinderwagen", re.IGNORECASE),
    re.compile(r"Aufzu(g|üge).*?außer Betrieb", re.IGNORECASE),
]

BASE_LINE_MODE = {
    "U1": "U", "U2": "U", "U3": "U", "U4": "U",
    "S1": "S", "S2": "S", "S3": "S", "S4": "S", "S5": "S", "S6": "S", "S7": "S",
    "A1": "AKN", "A2": "AKN", "A3": "AKN",
}


def is_accessibility_related(announcement: dict) -> bool:
    description = announcement.get("description") or ""
    return any(p.search(description) for p in ACCESSIBILITY_PATTERNS)


def classify_category(announcement: dict) -> str:
    """SPERRUNG: clear closure/replacement-service (title keyword match -
    titles are more precise than body text, which often phrases things
    differently, e.g. 'verkehren keine Züge' instead of 'gesperrt').
    BARRIEREFREIHEIT: pure accessibility notice. SONSTIGE: everything else."""
    if is_accessibility_related(announcement):
        return CATEGORY_BARRIEREFREIHEIT
    summary = announcement.get("summary") or ""
    if "Sperrung" in summary or "Ersatzverkehr" in summary:
        return CATEGORY_SPERRUNG
    return CATEGORY_SONSTIGE


def is_currently_valid(announcement: dict, now: datetime | None = None) -> bool:
    """True if `now` falls within any of the announcement's validity windows.
    No validities at all -> treated as valid (conservative)."""
    if now is None:
        now = datetime.now(timezone.utc)
    validities = announcement.get("validities") or []
    if not validities:
        return True
    for time_range in validities:
        begin, end = time_range.get("begin"), time_range.get("end")
        if not begin or not end:
            continue
        if datetime.fromisoformat(begin) <= now <= datetime.fromisoformat(end):
            return True
    return False


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
