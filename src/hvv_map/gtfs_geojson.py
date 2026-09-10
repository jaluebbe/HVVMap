"""Build GeoJSON layers (vehicle positions, reference lines/stops) from a
loaded GTFS Schedule - purely fahrplan-based, no GTI credentials.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from hvv_map.gtfs_schedule import (
    MODEL_BY_ROUTE_TYPE,
    Schedule,
    active_service_ids,
    find_current_segment,
    head_position,
    mode_for_trip,
    recently_active_service_ids,
    recently_active_shape_ids,
)

BERLIN_TZ = ZoneInfo("Europe/Berlin")

ICON_HEIGHT = 10
MARKER_RADIUS = 2.5
# Deliberately below the median observed real turnaround time at a terminus,
# to avoid showing a vehicle that's already gone while a genuine, much
# quicker follow-up trip is arriving at the same stop - ported from the
# earlier hvv_gtfs.py experiment, where this value was tuned against real
# HVV data (see occupied-stops dedup below).
ORIGIN_LEAD_SECONDS = 60

# route_short_name -> hex color (no leading '#'). GTFS route_color is blank
# for most HVV routes, so this has to be maintained by hand - ported from
# the earlier hvv_gtfs.py experiment. Consolidating with hvv_map.lines'
# LINE_COLORS is a possible future cleanup, once its keys are confirmed to
# use the same route_short_name-style names.
LINE_COLORS = {
    "U1": "006ab3",
    "U2": "e2001a",
    "U3": "ffdd00",
    "U4": "0098a1",
    "S1": "1a962b",
    "S2": "b51143",
    "S3": "622181",
    "S5": "0089bb",
    "S7": "cc7720",
    "A1": "F29400",
    "A2": "F29400",
    "A3": "F29400",
    # Ferry: route_color is blank/white in GTFS, using route_text_color
    # (matches the official ferry icon's fill #009ED4).
    "61": "009DD1",
    "62": "009DD1",
    "64": "009DD1",
    "65": "009DD1",
    "72": "009DD1",
    "73": "009DD1",
    "75": "009DD1",
    "U1-DIREKT": "E2001A",
    "U1-ERSATZ": "E2001A",
    "A1-SEV": "E2001A",
    "A2-SEV": "E2001A",
    "A3-Bus": "E2001A",
    "S5-SEV": "E2001A",
}
# geofox icon service lineKey format. GTFS route_short_name has no
# equivalent field, so this mapping has to be maintained by hand - ported
# from the earlier hvv_gtfs.py experiment.
LINE_ID_MAP = {
    "U1": "HHA-U:U1_HHA-U",
    "U2": "HHA-U:U2_HHA-U",
    "U3": "HHA-U:U3_HHA-U",
    "U4": "HHA-U:U4_HHA-U",
    "S1": "ZVU-DB:S1_ZVU-DB_SBHZVU",
    "S2": "SBH:S2_SBH_SBAHNS",
    "S3": "SBH:S3_SBH_SBAHNS",
    "S5": "SBH:S5_SBH_SBAHNS",
    "S7": "SBH:S7_SBH_SBAHNS",
    "A1": "AKN:A1_AKN_AKN___",
    "A2": "AKN:A2_AKN_AKN___",
    "A3": "AKN:A3_AKN_AKN___",
    "61": "ZVU-DB:61_ZVU-DB_HADAGZ",
    "62": "ZVU-DB:62_ZVU-DB_HADAGZ",
    "64": "ZVU-DB:64_ZVU-DB_HADAGZ",
    "65": "ZVU-DB:65_ZVU-DB_HADAGZ",
    "72": "ZVU-DB:72_ZVU-DB_HADAGZ",
    "73": "ZVU-DB:73_ZVU-DB_HADAGZ",
    "75": "ZVU-DB:75_ZVU-DB_HADAGZ",
    "U1-DIREKT": "HHA-B:U1-DIREKT_HHA-B",
    "U1-ERSATZ": "HHA-B:U1-ERSATZ_HHA-B",
    "A1-SEV": "DB-EFZ:A1-SEV_DB-EFZ_Z",
    "A2-SEV": "DB-EFZ:A2-SEV_DB-EFZ_Z",
    "A3-Bus": "VHH:A3-Bus_VHH",
    "S5-SEV": "SBH:S5-SEV_SBH_SBAHNS",
}


def _stop_coord(stop: dict):
    return (float(stop["stop_lon"]), float(stop["stop_lat"]))


def _build_point_feature(
    journey_id, line_short_name, color, head, text, progress, direction, mode
):
    icon_key = LINE_ID_MAP.get(line_short_name)
    label = direction.split("(")[0].strip()
    if icon_key:
        properties = {
            "journeyID": journey_id,
            "icon": f"https://cloud.geofox.de/icon/line?height={ICON_HEIGHT}&lineKey={icon_key}&fileFormat=SVG",
            "iconHeight": ICON_HEIGHT,
            "line": line_short_name,
            "label": label,
            "text": text,
            "progress": round(progress, 3),
            "mode": mode,
            "_route": line_short_name,
        }
    else:
        properties = {
            "journeyID": journey_id,
            "color": color,
            "fill": True,
            "markerRadius": MARKER_RADIUS,
            "fillOpacity": 1,
            "label": label,
            "text": text,
            "progress": round(progress, 3),
            "mode": mode,
            "_route": line_short_name,
        }
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": list(head)},
        "properties": properties,
    }


def _merge_colocated_features(features: list) -> list:
    """Merges points with identical coordinate AND line into one symbol -
    e.g. coupled/portioned trains that share a position until they split
    (like S1 towards Poppenbüttel/Airport). Without this, two markers would
    sit exactly on top of each other."""
    groups: dict = {}
    order = []
    for f in features:
        coords = tuple(round(c, 6) for c in f["geometry"]["coordinates"])
        route = f["properties"].get("_route")
        key = (coords, route)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)

    merged = []
    for key in order:
        group = groups[key]
        for f in group:
            f["properties"].pop("_route", None)

        if len(group) == 1:
            merged.append(group[0])
            continue

        base = group[0]
        labels, directions = [], []
        for f in group:
            label = f["properties"].get("label")
            if label and label not in labels:
                labels.append(label)
            direction_part = (f["properties"].get("text") or "").split("<br>")[0]
            if direction_part and direction_part not in directions:
                directions.append(direction_part)

        base_text_parts = (base["properties"].get("text") or "").split("<br>")
        model = base_text_parts[1] if len(base_text_parts) > 1 else ""
        combined_direction = " / ".join(directions)

        merged_properties = dict(base["properties"])
        merged_properties["label"] = " / ".join(labels)
        merged_properties["text"] = (
            f"{combined_direction}<br>{model}" if model else combined_direction
        )
        merged_properties["journeyID"] = "+".join(
            f["properties"]["journeyID"] for f in group
        )

        merged.append(
            {
                "type": "Feature",
                "geometry": base["geometry"],
                "properties": merged_properties,
            }
        )
    return merged


def build_positions_geojson(schedule: Schedule, now_ts: int) -> dict:
    now_local = datetime.fromtimestamp(now_ts, tz=BERLIN_TZ)
    today = now_local.date()
    yesterday = today - timedelta(days=1)

    active_today = active_service_ids(schedule, today)
    active_yesterday = active_service_ids(schedule, yesterday)

    midnight_today = datetime(today.year, today.month, today.day, tzinfo=BERLIN_TZ)
    base_epoch_today = int(midnight_today.timestamp())
    target_seconds_today = now_ts - base_epoch_today
    target_seconds_yesterday = target_seconds_today + 86400

    features = []
    occupied_stops = set()
    unmatched_trips = []

    for trip_id, rows in schedule.trip_stop_times.items():
        trip = schedule.trips[trip_id]
        service_id = trip["service_id"]

        candidates = []
        if service_id in active_today:
            candidates.append((target_seconds_today, base_epoch_today))
        if service_id in active_yesterday:
            candidates.append((target_seconds_yesterday, base_epoch_today - 86400))
        if not candidates:
            continue

        segment = chosen_base_epoch = None
        for cand_seconds, cand_base_epoch in candidates:
            found = find_current_segment(rows, cand_seconds)
            if found is not None:
                segment = found
                chosen_base_epoch = cand_base_epoch
                break
        if segment is None:
            unmatched_trips.append((trip_id, rows, trip, candidates))
            continue

        prev, nxt = segment
        route = schedule.routes[trip["route_id"]]
        mode = mode_for_trip(schedule, trip)
        line_short_name = route["route_short_name"]
        color_hex = LINE_COLORS.get(line_short_name)
        if color_hex is None:
            continue

        start_stop = schedule.stops.get(prev[1])
        end_stop = schedule.stops.get(nxt[1])
        if not start_stop or not end_stop:
            continue
        occupied_stops.add(prev[1])
        occupied_stops.add(nxt[1])

        coords = [_stop_coord(start_stop), _stop_coord(end_stop)]
        actual_start = chosen_base_epoch + prev[3]
        actual_end = chosen_base_epoch + nxt[2]
        result = head_position(coords, actual_start, actual_end, now_ts)
        if result is None:
            continue
        progress, head = result

        model = MODEL_BY_ROUTE_TYPE.get(route["route_type"], "")
        text = "<br>".join([trip["trip_headsign"], model])
        journey_id = f"GTFS:{trip['route_id']}.{trip_id}"
        features.append(
            _build_point_feature(
                journey_id,
                line_short_name,
                f"#{color_hex}",
                head,
                text,
                progress,
                trip["trip_headsign"],
                mode,
            )
        )

    # Trips about to depart for the first time appear ORIGIN_LEAD_SECONDS
    # early at their origin stop - but only if that platform isn't already
    # occupied by another active vehicle (avoids duplicates on very short
    # real turnarounds).
    for trip_id, rows, trip, candidates in unmatched_trips:
        route = schedule.routes[trip["route_id"]]
        line_short_name = route["route_short_name"]
        color_hex = LINE_COLORS.get(line_short_name)
        if color_hex is None:
            continue

        first_stop_id = rows[0][1]
        if first_stop_id in occupied_stops:
            continue

        first_departure = rows[0][3]
        for cand_seconds, _ in candidates:
            if first_departure - ORIGIN_LEAD_SECONDS <= cand_seconds < first_departure:
                stop = schedule.stops.get(first_stop_id)
                if stop:
                    model = MODEL_BY_ROUTE_TYPE.get(route["route_type"], "")
                    text = "<br>".join([trip["trip_headsign"], model])
                    journey_id = f"GTFS:{trip['route_id']}.{trip_id}"
                    features.append(
                        _build_point_feature(
                            journey_id,
                            line_short_name,
                            f"#{color_hex}",
                            _stop_coord(stop),
                            text,
                            0.0,
                            trip["trip_headsign"],
                            mode_for_trip(schedule, trip),
                        )
                    )
                    occupied_stops.add(first_stop_id)
                break

    return {
        "type": "FeatureCollection",
        "features": _merge_colocated_features(features),
    }


def build_stops_geojson(schedule: Schedule, reference_date: date | None = None) -> dict:
    reference_date = reference_date or datetime.now(tz=BERLIN_TZ).date()
    service_ids = recently_active_service_ids(schedule, reference_date)

    modes_by_stop: dict = {}
    for trip in schedule.trips.values():
        if trip["service_id"] not in service_ids:
            continue
        mode = mode_for_trip(schedule, trip)
        if not mode:
            continue
        for _, stop_id, _, _ in schedule.trip_stop_times.get(trip["trip_id"], []):
            modes_by_stop.setdefault(stop_id, set()).add(mode)

    features = []
    for stop_id, modes in modes_by_stop.items():
        stop = schedule.stops.get(stop_id)
        if not stop:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": list(_stop_coord(stop))},
                "properties": {
                    "text": stop.get("stop_name", ""),
                    "modes": sorted(modes),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def build_lines_geojson(schedule: Schedule, reference_date: date | None = None) -> dict:
    """Draws each recently-active trip's full shapes.txt polyline directly -
    no stop-to-stop straight lines, so curves render correctly. A shape_id
    is effectively unique per route+direction in practice, so one feature is
    built per shape, taking color from any trip that uses it; "modes" is the
    union across all trips sharing it in case that assumption doesn't hold.

    Replacement-bus shapes (SPECIAL_BUS_ROUTES) are often just a straight
    start/end line in the feed, unlike the rail shapes - shown as-is for now
    (a point-to-point line), not worth the complexity of a fallback.
    """
    reference_date = reference_date or datetime.now(tz=BERLIN_TZ).date()
    shape_ids = recently_active_shape_ids(schedule, reference_date)

    info_by_shape: dict = {}
    for trip in schedule.trips.values():
        shape_id = trip.get("shape_id")
        if shape_id not in shape_ids:
            continue
        route = schedule.routes.get(trip["route_id"])
        if not route:
            continue
        mode = mode_for_trip(schedule, trip)
        if not mode:
            continue
        color_hex = LINE_COLORS.get(route["route_short_name"])
        if color_hex is None:
            continue
        entry = info_by_shape.setdefault(
            shape_id, {"color": f"#{color_hex}", "modes": set()}
        )
        entry["modes"].add(mode)

    features = []
    for shape_id, info in info_by_shape.items():
        coords = schedule.shapes.get(shape_id)
        if not coords or len(coords) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [list(c) for c in coords],
                },
                "properties": {"color": info["color"], "modes": sorted(info["modes"])},
            }
        )
    return {"type": "FeatureCollection", "features": features}
