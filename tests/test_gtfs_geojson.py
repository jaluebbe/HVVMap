from datetime import date, datetime

from hvv_map.gtfs_geojson import (
    BERLIN_TZ,
    build_lines_geojson,
    build_positions_geojson,
    build_stops_geojson,
)
from hvv_map.gtfs_schedule import Schedule


def _epoch(y, m, d, hh, mm, ss=0):
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=BERLIN_TZ).timestamp())


def _make_schedule(
    trips=None, trip_stop_times=None, extra_routes=None, shapes=None, calendar_rows=None,
):
    routes = {"R1": {"route_id": "R1", "route_short_name": "U1", "route_type": "402"}}
    if extra_routes:
        routes.update(extra_routes)
    stops = {
        "A": {"stop_id": "A", "stop_name": "Alpha", "stop_lat": "53.50", "stop_lon": "10.00"},
        "B": {"stop_id": "B", "stop_name": "Beta", "stop_lat": "53.52", "stop_lon": "10.00"},
    }
    trips = trips or {
        "T1": {
            "trip_id": "T1", "route_id": "R1", "service_id": "ALLDAYS", "trip_headsign": "Beta",
            "shape_id": "SHAPE1",
        },
    }
    trip_stop_times = trip_stop_times or {
        "T1": [(1, "A", 28800, 28800), (2, "B", 28920, 28920)],  # 08:00:00 -> 08:02:00
    }
    calendar_rows = calendar_rows or [
        {
            "service_id": "ALLDAYS",
            "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1", "friday": "1",
            "saturday": "1", "sunday": "1",
            "start_date": "20200101", "end_date": "20301231",
        },
    ]
    return Schedule(
        routes=routes,
        stops=stops,
        trips=trips,
        trip_stop_times=trip_stop_times,
        calendar_rows=calendar_rows,
        calendar_dates={},
        shapes=shapes if shapes is not None else {"SHAPE1": [(10.00, 53.50), (10.00, 53.52)]},
    )


def test_build_positions_geojson_places_vehicle_between_stops():
    schedule = _make_schedule()
    now_ts = _epoch(2025, 6, 2, 8, 1, 0)  # midpoint of the 08:00-08:02 segment

    result = build_positions_geojson(schedule, now_ts)

    assert len(result["features"]) == 1
    feature = result["features"][0]
    assert feature["properties"]["mode"] == "U"
    assert feature["properties"]["line"] == "U1"
    assert 0.4 < feature["properties"]["progress"] < 0.6


def test_build_positions_geojson_skips_route_without_known_color():
    schedule = _make_schedule(
        trips={"T1": {"trip_id": "T1", "route_id": "R2", "service_id": "ALLDAYS", "trip_headsign": "Beta"}},
        extra_routes={"R2": {"route_id": "R2", "route_short_name": "U99", "route_type": "402"}},
    )
    now_ts = _epoch(2025, 6, 2, 8, 1, 0)

    result = build_positions_geojson(schedule, now_ts)

    assert result["features"] == []


def test_build_positions_geojson_shows_origin_lead_time_vehicle():
    schedule = _make_schedule()
    # 30s before the 08:00:00 first departure - within ORIGIN_LEAD_SECONDS (60s).
    now_ts = _epoch(2025, 6, 2, 7, 59, 30)

    result = build_positions_geojson(schedule, now_ts)

    assert len(result["features"]) == 1
    assert result["features"][0]["properties"]["progress"] == 0.0


def test_build_positions_geojson_empty_outside_any_trip_window():
    schedule = _make_schedule()
    now_ts = _epoch(2025, 6, 2, 12, 0, 0)  # long after the only trip finished

    result = build_positions_geojson(schedule, now_ts)

    assert result["features"] == []


def test_build_stops_geojson_includes_modes_for_used_stops():
    schedule = _make_schedule()
    result = build_stops_geojson(schedule, reference_date=date(2025, 6, 2))

    assert len(result["features"]) == 2
    for feature in result["features"]:
        assert feature["properties"]["modes"] == ["U"]


def test_build_stops_geojson_excludes_stop_without_recent_trip():
    old_calendar = [
        {
            "service_id": "OLD",
            "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1", "friday": "1",
            "saturday": "1", "sunday": "1",
            "start_date": "20200101", "end_date": "20200601",  # long expired
        },
    ]
    schedule = _make_schedule(
        trips={
            "T1": {
                "trip_id": "T1", "route_id": "R1", "service_id": "OLD", "trip_headsign": "Beta",
                "shape_id": "SHAPE1",
            },
        },
        calendar_rows=old_calendar,
    )

    result = build_stops_geojson(schedule, reference_date=date(2025, 6, 2))

    assert result["features"] == []


def test_build_lines_geojson_draws_full_shape_not_straight_stop_line():
    curved_shape = {"SHAPE1": [(10.00, 53.50), (10.01, 53.51), (10.00, 53.52)]}
    schedule = _make_schedule(shapes=curved_shape)

    result = build_lines_geojson(schedule, reference_date=date(2025, 6, 2))

    assert len(result["features"]) == 1
    feature = result["features"][0]
    assert feature["geometry"]["type"] == "LineString"
    assert feature["geometry"]["coordinates"] == [[10.00, 53.50], [10.01, 53.51], [10.00, 53.52]]
    assert feature["properties"]["modes"] == ["U"]


def test_build_lines_geojson_excludes_shape_without_recent_trip():
    old_calendar = [
        {
            "service_id": "OLD",
            "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1", "friday": "1",
            "saturday": "1", "sunday": "1",
            "start_date": "20200101", "end_date": "20200601",  # long expired
        },
    ]
    schedule = _make_schedule(
        trips={
            "T1": {
                "trip_id": "T1", "route_id": "R1", "service_id": "OLD", "trip_headsign": "Beta",
                "shape_id": "SHAPE1",
            },
        },
        calendar_rows=old_calendar,
    )

    result = build_lines_geojson(schedule, reference_date=date(2025, 6, 2))

    assert result["features"] == []
