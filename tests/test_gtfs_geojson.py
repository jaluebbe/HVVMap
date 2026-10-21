import math
from datetime import date, datetime

from hvv_map.gtfs_geojson import (
    BERLIN_TZ,
    build_departures_tree,
    build_lines_geojson,
    build_positions_geojson,
    build_stops_geojson,
)
from hvv_map.gtfs_schedule import Schedule


def _epoch(y, m, d, hh, mm, ss=0):
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=BERLIN_TZ).timestamp())


def _make_schedule(
    trips=None,
    trip_stop_times=None,
    extra_routes=None,
    shapes=None,
    calendar_rows=None,
):
    routes = {"R1": {"route_id": "R1", "route_short_name": "U1", "route_type": "402"}}
    if extra_routes:
        routes.update(extra_routes)
    stops = {
        "A": {
            "stop_id": "A",
            "stop_name": "Alpha",
            "stop_lat": "53.50",
            "stop_lon": "10.00",
        },
        "B": {
            "stop_id": "B",
            "stop_name": "Beta",
            "stop_lat": "53.52",
            "stop_lon": "10.00",
        },
    }
    trips = trips or {
        "T1": {
            "trip_id": "T1",
            "route_id": "R1",
            "service_id": "ALLDAYS",
            "trip_headsign": "Beta",
            "shape_id": "SHAPE1",
        },
    }
    trip_stop_times = trip_stop_times or {
        "T1": [(1, "A", 28800, 28800), (2, "B", 28920, 28920)],  # 08:00:00 -> 08:02:00
    }
    calendar_rows = calendar_rows or [
        {
            "service_id": "ALLDAYS",
            "monday": "1",
            "tuesday": "1",
            "wednesday": "1",
            "thursday": "1",
            "friday": "1",
            "saturday": "1",
            "sunday": "1",
            "start_date": "20200101",
            "end_date": "20301231",
        },
    ]
    return Schedule(
        routes=routes,
        stops=stops,
        trips=trips,
        trip_stop_times=trip_stop_times,
        calendar_rows=calendar_rows,
        calendar_dates={},
        shapes=shapes
        if shapes is not None
        else {"SHAPE1": [(10.00, 53.50), (10.00, 53.52)]},
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


def test_build_positions_geojson_follows_curved_shape_not_straight_line():
    # A detour via (10.02, 53.51) that a straight line between Alpha
    # (10.00, 53.50) and Beta (10.00, 53.52) would never pass through.
    curved_shape = {"SHAPE1": [(10.00, 53.50), (10.02, 53.51), (10.00, 53.52)]}
    schedule = _make_schedule(shapes=curved_shape)
    now_ts = _epoch(2025, 6, 2, 8, 1, 0)  # midpoint of the 08:00-08:02 segment

    result = build_positions_geojson(schedule, now_ts)

    lon, _lat = result["features"][0]["geometry"]["coordinates"]
    assert lon > 10.005  # only possible if the detour point was used


def test_build_positions_geojson_falls_back_to_straight_line_without_shape():
    schedule = _make_schedule(
        trips={
            "T1": {
                "trip_id": "T1",
                "route_id": "R1",
                "service_id": "ALLDAYS",
                "trip_headsign": "Beta",
                "shape_id": "MISSING_SHAPE",
            },
        }
    )
    now_ts = _epoch(2025, 6, 2, 8, 1, 0)

    result = build_positions_geojson(schedule, now_ts)

    lon, _lat = result["features"][0]["geometry"]["coordinates"]
    assert lon == 10.00  # straight line between two same-longitude stops


def test_build_positions_geojson_skips_route_without_known_color():
    schedule = _make_schedule(
        trips={
            "T1": {
                "trip_id": "T1",
                "route_id": "R2",
                "service_id": "ALLDAYS",
                "trip_headsign": "Beta",
            }
        },
        extra_routes={
            "R2": {"route_id": "R2", "route_short_name": "U99", "route_type": "402"}
        },
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


def test_build_stops_geojson_includes_lines_for_used_stops():
    schedule = _make_schedule()
    result = build_stops_geojson(schedule, reference_date=date(2025, 6, 2))

    for feature in result["features"]:
        assert feature["properties"]["lines"] == ["U1"]


def test_build_stops_geojson_name_matches_stop_name():
    schedule = _make_schedule()
    result = build_stops_geojson(schedule, reference_date=date(2025, 6, 2))

    names = {feature["properties"]["name"] for feature in result["features"]}
    assert names == {"Alpha", "Beta"}
    for feature in result["features"]:
        # "name" backs the map's persistent station labels (text-field);
        # "text" backs the hover tooltip - same value here, but kept as two
        # properties since the two consumers are independent.
        assert feature["properties"]["name"] == feature["properties"]["text"]


def test_build_stops_geojson_excludes_stop_without_recent_trip():
    old_calendar = [
        {
            "service_id": "OLD",
            "monday": "1",
            "tuesday": "1",
            "wednesday": "1",
            "thursday": "1",
            "friday": "1",
            "saturday": "1",
            "sunday": "1",
            "start_date": "20200101",
            "end_date": "20200601",  # long expired
        },
    ]
    schedule = _make_schedule(
        trips={
            "T1": {
                "trip_id": "T1",
                "route_id": "R1",
                "service_id": "OLD",
                "trip_headsign": "Beta",
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
    assert feature["geometry"]["coordinates"] == [
        [10.00, 53.50],
        [10.01, 53.51],
        [10.00, 53.52],
    ]
    assert feature["properties"]["modes"] == ["U"]


def test_build_lines_geojson_excludes_shape_without_recent_trip():
    old_calendar = [
        {
            "service_id": "OLD",
            "monday": "1",
            "tuesday": "1",
            "wednesday": "1",
            "thursday": "1",
            "friday": "1",
            "saturday": "1",
            "sunday": "1",
            "start_date": "20200101",
            "end_date": "20200601",  # long expired
        },
    ]
    schedule = _make_schedule(
        trips={
            "T1": {
                "trip_id": "T1",
                "route_id": "R1",
                "service_id": "OLD",
                "trip_headsign": "Beta",
                "shape_id": "SHAPE1",
            },
        },
        calendar_rows=old_calendar,
    )

    result = build_lines_geojson(schedule, reference_date=date(2025, 6, 2))

    assert result["features"] == []


def _dense_track(points=40, bulge=0.02):
    """A curved track between Alpha and Beta with ~50 m point spacing."""
    return [
        (
            10.00 + bulge * math.sin(math.pi * i / (points - 1)),
            53.50 + 0.02 * i / (points - 1),
        )
        for i in range(points)
    ]


def test_sparse_shape_is_flagged_straight_for_positions_and_lines():
    schedule = _make_schedule()  # default shape: just the two stops, 2 km apart
    now_ts = _epoch(2025, 6, 2, 8, 1, 0)

    position = build_positions_geojson(schedule, now_ts)["features"][0]
    line = build_lines_geojson(schedule, reference_date=date(2025, 6, 2))["features"][0]

    assert position["properties"]["straight"] is True
    assert line["properties"]["straight"] is True


def test_dense_track_is_not_straight():
    schedule = _make_schedule(shapes={"SHAPE1": _dense_track()})
    now_ts = _epoch(2025, 6, 2, 8, 1, 0)

    position = build_positions_geojson(schedule, now_ts)["features"][0]
    lines = build_lines_geojson(schedule, reference_date=date(2025, 6, 2))["features"]

    assert position["properties"]["straight"] is False
    assert [f["properties"]["straight"] for f in lines] == [False]


def test_long_hop_inside_a_dense_track_is_split_off_as_straight_line():
    track = _dense_track()
    gap = [(track[-1][0], track[-1][1]), (10.00, 53.60)]  # ~11 km without a track
    schedule = _make_schedule(shapes={"SHAPE1": track + gap[1:]})

    lines = build_lines_geojson(schedule, reference_date=date(2025, 6, 2))["features"]

    assert [f["properties"]["straight"] for f in lines] == [False, True]
    assert (
        lines[0]["geometry"]["coordinates"][-1]
        == lines[1]["geometry"]["coordinates"][0]
    )


def test_icon_key_is_derived_for_regional_lines_and_their_sev():
    from hvv_map.gtfs_geojson import icon_key_for

    assert icon_key_for("RE1") == "DB-EFZ:RE1_DB-EFZ_Z"
    assert icon_key_for("RE6x-SEV") == "DB-EFZ:RE6x-SEV_DB-EFZ_Z"
    assert icon_key_for("S5-SEV") == "SBH:S5-SEV_SBH_SBAHNS"
    assert icon_key_for("RE74") is None


def test_long_hops_are_straight_links_except_on_regional_shapes():
    from hvv_map.gtfs_geojson import _long_hop_km, _straight_hops

    # dense track with one 5 km hop (0.045 deg lat ~ 5 km)
    coords = [[10.0, 53.0 + i * 0.0003] for i in range(100)]
    coords.append([10.0, 53.03 + 0.045])
    coords.append([10.0, 53.03 + 0.0453])
    assert any(_straight_hops(coords, _long_hop_km({"U"})))
    assert not any(_straight_hops(coords, _long_hop_km({"R"})))
    # a hop far beyond any real track section is still straight
    coords[-1] = [10.0, 53.03 + 0.5]
    assert any(_straight_hops(coords, _long_hop_km({"R"})))


def test_regional_segment_with_two_distant_shape_points_is_not_straight():
    from hvv_map.gtfs_geojson import _segment_is_straight

    shape = [[10.0, 53.0 + i * 0.0003] for i in range(200)]
    segment = [[10.0, 53.0], [10.0, 53.045]]
    assert not _segment_is_straight(shape, segment, "R")
    assert _segment_is_straight(shape, segment, "U")
    assert _segment_is_straight([], segment, "R")
    sparse = [[10.0, 53.0 + i * 0.045] for i in range(5)]
    assert _segment_is_straight(sparse, sparse[:2], "R")
    assert _segment_is_straight(shape, [[10.0, 53.0], [10.0, 53.5]], "R")


def test_replacement_bus_chord_of_over_a_kilometre_is_straight():
    from hvv_map.gtfs_geojson import _long_hop_km, _straight_hops

    dense = [[10.0, 53.0 + i * 0.0003] for i in range(100)]
    coords = dense + [[10.0, 53.03 + 0.011]]  # 1.2 km chord after dense points
    assert any(_straight_hops(coords, _long_hop_km({"AKN"}, ["A2-SEV"])))
    assert not any(_straight_hops(coords, _long_hop_km({"AKN"}, ["A2"])))


def test_ubahn_chord_of_over_a_kilometre_is_straight_but_not_on_other_modes():
    from hvv_map.gtfs_geojson import _long_hop_km, _straight_hops

    dense = [[10.0, 53.0 + i * 0.0003] for i in range(100)]
    coords = dense + [[10.0, 53.03 + 0.014]]  # 1.5 km chord
    assert any(_straight_hops(coords, _long_hop_km({"U"})))
    assert not any(_straight_hops(coords, _long_hop_km({"AKN"})))


def _two_shape_schedule(dense_shape):
    trips = {
        "T1": {
            "trip_id": "T1",
            "route_id": "R1",
            "service_id": "ALLDAYS",
            "trip_headsign": "Beta",
            "shape_id": "SPARSE",
        },
        "T2": {
            "trip_id": "T2",
            "route_id": "R1",
            "service_id": "ALLDAYS",
            "trip_headsign": "Beta",
            "shape_id": "DENSE",
        },
    }
    times = [(1, "A", 28800, 28800), (2, "B", 28920, 28920)]
    return _make_schedule(
        trips=trips,
        trip_stop_times={"T1": times, "T2": times},
        shapes={"SPARSE": [(10.0, 53.50), (10.0, 53.52)], "DENSE": dense_shape},
    )


def test_straight_run_is_dropped_where_another_shape_has_a_real_track():
    schedule = _two_shape_schedule(_dense_track())

    lines = build_lines_geojson(schedule, reference_date=date(2025, 6, 2))["features"]

    assert [f["properties"]["straight"] for f in lines] == [False]


def test_straight_run_stays_without_a_real_track_between_its_end_points():
    schedule = _two_shape_schedule([(10.0, 53.50), (10.0, 53.52)])

    lines = build_lines_geojson(schedule, reference_date=date(2025, 6, 2))["features"]

    assert [f["properties"]["straight"] for f in lines] == [True, True]


def test_straight_run_stays_when_the_real_track_is_elsewhere():
    far = [(10.5 + 0.0005 * i, 53.80) for i in range(60)]
    schedule = _two_shape_schedule(far)

    lines = build_lines_geojson(schedule, reference_date=date(2025, 6, 2))["features"]

    assert sorted(f["properties"]["straight"] for f in lines) == [False, True]


def test_departures_tree_lists_each_stop_but_the_last_with_its_headsign():
    tree = build_departures_tree(_make_schedule(), date(2025, 6, 2))

    assert tree["anchor"] == "2025-06-02 00:00"
    [mode] = tree["modes"]
    [line] = mode["lines"]
    assert (mode["mode"], line["name"]) == ("U", "U1")
    [station] = line["stations"]  # Beta is the last stop: no departure
    assert station["name"] == "Alpha"
    assert station["departures"] == [
        {
            "offset": 480,
            "time": "08:00",
            "next_day": False,
            "direction": "Beta",
            "platform": None,
            "trip_id": "T1",
        }
    ]


def test_departures_tree_takes_trips_past_midnight_from_the_day_before():
    schedule = _make_schedule(
        trip_stop_times={"T1": [(1, "A", 87000, 87000), (2, "B", 87600, 87600)]}
    )  # 24:10 -> 24:20 of the service day

    tree = build_departures_tree(schedule, date(2025, 6, 2))

    departures = tree["modes"][0]["lines"][0]["stations"][0]["departures"]
    assert [(d["time"], d["offset"]) for d in departures][0] == ("00:10", 10)
