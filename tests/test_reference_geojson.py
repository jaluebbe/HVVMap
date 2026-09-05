from hvv_map.lines import SublineInfo
from hvv_map.reference_geojson import (
    build_lines_geojson,
    build_stops_geojson,
    mode_for_subline,
)
from hvv_map.segment_cache import open_cache, record_line_usage, store
from hvv_map.stations import StationInfo

STATIONS = {
    "Master:1": StationInfo(
        id="Master:1", name="Ohlstedt", city="Hamburg", lon=10.1, lat=53.7
    ),
    "Master:2": StationInfo(
        id="Master:2", name="Volksdorf", city="Hamburg", lon=10.15, lat=53.68
    ),
}


def test_mode_for_subline_normal_vehicle_type():
    subline = SublineInfo(
        line_name="U1", line_id="HHA-U:U1_HHA-U", subline_number="1",
        vehicle_type="U_BAHN", station_ids=(),
    )  # fmt: skip
    assert mode_for_subline(subline) == "U"


def test_mode_for_subline_replacement_bus():
    subline = SublineInfo(
        line_name="U1-ERSATZ", line_id="HHA-B:U1-ERSATZ_HHA-B", subline_number="1",
        vehicle_type="REGIONALBUS", station_ids=(),
    )  # fmt: skip
    assert mode_for_subline(subline) == "U"


def test_mode_for_subline_unknown_replacement_bus_returns_empty():
    subline = SublineInfo(
        line_name="X", line_id="unknown:id", subline_number="1",
        vehicle_type="REGIONALBUS", station_ids=(),
    )  # fmt: skip
    assert mode_for_subline(subline) == ""


def test_build_stops_geojson_one_feature_per_station():
    subline = SublineInfo(
        line_name="U1", line_id="HHA-U:U1_HHA-U", subline_number="1",
        vehicle_type="U_BAHN", station_ids=("Master:1", "Master:2"),
    )  # fmt: skip
    result = build_stops_geojson([subline], STATIONS)
    assert len(result["features"]) == 2


def test_build_stops_geojson_geometry_and_properties():
    subline = SublineInfo(
        line_name="U1", line_id="HHA-U:U1_HHA-U", subline_number="1",
        vehicle_type="U_BAHN", station_ids=("Master:1",),
    )  # fmt: skip
    feature = build_stops_geojson([subline], STATIONS)["features"][0]
    assert feature["geometry"] == {"type": "Point", "coordinates": [10.1, 53.7]}
    assert feature["properties"] == {
        "id": "Master:1",
        "name": "Ohlstedt",
        "modes": ["U"],
        "text": "Ohlstedt",
    }


def test_build_stops_geojson_combines_modes_at_interchange():
    u1 = SublineInfo(
        line_name="U1", line_id="HHA-U:U1_HHA-U", subline_number="1",
        vehicle_type="U_BAHN", station_ids=("Master:1",),
    )  # fmt: skip
    s1 = SublineInfo(
        line_name="S1", line_id="ZVU-DB:S1_ZVU-DB_SBHZVU", subline_number="1",
        vehicle_type="S_BAHN", station_ids=("Master:1",),
    )  # fmt: skip
    feature = build_stops_geojson([u1, s1], STATIONS)["features"][0]
    assert feature["properties"]["modes"] == ["S", "U"]


def test_build_stops_geojson_skips_stations_without_coordinates():
    subline = SublineInfo(
        line_name="U1", line_id="HHA-U:U1_HHA-U", subline_number="1",
        vehicle_type="U_BAHN", station_ids=("Master:unknown",),
    )  # fmt: skip
    result = build_stops_geojson([subline], STATIONS)
    assert result["features"] == []


def test_build_stops_geojson_without_active_names_keeps_everything():
    subline = SublineInfo(
        line_name="U1", line_id="HHA-U:U1_HHA-U", subline_number="1",
        vehicle_type="U_BAHN", station_ids=("Master:1",),
    )  # fmt: skip
    result = build_stops_geojson([subline], STATIONS)
    assert len(result["features"]) == 1


def test_build_stops_geojson_keeps_station_in_active_names():
    subline = SublineInfo(
        line_name="U1", line_id="HHA-U:U1_HHA-U", subline_number="1",
        vehicle_type="U_BAHN", station_ids=("Master:1",),
    )  # fmt: skip
    result = build_stops_geojson([subline], STATIONS, active_names={"Ohlstedt"})
    assert len(result["features"]) == 1


def test_build_stops_geojson_drops_station_not_in_active_names():
    subline = SublineInfo(
        line_name="U1", line_id="HHA-U:U1_HHA-U", subline_number="1",
        vehicle_type="U_BAHN", station_ids=("Master:1",),
    )  # fmt: skip
    result = build_stops_geojson(
        [subline], STATIONS, active_names={"Irgendwas Anderes"}
    )
    assert result["features"] == []


def test_build_stops_geojson_empty_active_names_drops_all():
    subline = SublineInfo(
        line_name="U1", line_id="HHA-U:U1_HHA-U", subline_number="1",
        vehicle_type="U_BAHN", station_ids=("Master:1", "Master:2"),
    )  # fmt: skip
    result = build_stops_geojson([subline], STATIONS, active_names=set())
    assert result["features"] == []


def test_build_lines_geojson_converts_flat_track_to_coordinates(tmp_path):
    conn = open_cache(str(tmp_path / "cache.db"))
    store(conn, ("A", "B"), [10.0, 53.5, 10.01, 53.51])
    record_line_usage(conn, ("A", "B"), "U1", "U")

    feature = build_lines_geojson(conn)["features"][0]
    assert feature["geometry"] == {
        "type": "LineString",
        "coordinates": [[10.0, 53.5], [10.01, 53.51]],
    }


def test_build_lines_geojson_properties_list_lines_and_modes(tmp_path):
    conn = open_cache(str(tmp_path / "cache.db"))
    store(conn, ("A", "B"), [10.0, 53.5, 10.01, 53.51])
    record_line_usage(conn, ("A", "B"), "U1", "U")
    record_line_usage(conn, ("A", "B"), "U1-ERSATZ", "U")

    feature = build_lines_geojson(conn)["features"][0]
    assert feature["properties"] == {
        "lines": ["U1", "U1-ERSATZ"],
        "modes": ["U"],
        "color": "#006ab3",
    }


def test_build_lines_geojson_skips_segments_without_recorded_lines(tmp_path):
    conn = open_cache(str(tmp_path / "cache.db"))
    store(conn, ("A", "B"), [10.0, 53.5, 10.01, 53.51])  # geometry only
    result = build_lines_geojson(conn)
    assert result["features"] == []


def test_build_lines_geojson_skips_degenerate_single_point_tracks(tmp_path):
    conn = open_cache(str(tmp_path / "cache.db"))
    store(conn, ("A", "B"), [10.0, 53.5])  # only one point
    record_line_usage(conn, ("A", "B"), "U1", "U")
    result = build_lines_geojson(conn)
    assert result["features"] == []


def test_build_lines_geojson_color_matches_known_line(tmp_path):
    conn = open_cache(str(tmp_path / "cache.db"))
    store(conn, ("A", "B"), [10.0, 53.5, 10.01, 53.51])
    record_line_usage(conn, ("A", "B"), "S1", "S")
    feature = build_lines_geojson(conn)["features"][0]
    assert feature["properties"]["color"] == "#1a962b"


def test_build_lines_geojson_color_falls_back_for_unknown_line(tmp_path):
    conn = open_cache(str(tmp_path / "cache.db"))
    store(conn, ("A", "B"), [10.0, 53.5, 10.01, 53.51])
    record_line_usage(conn, ("A", "B"), "Metrobus 5", "")
    feature = build_lines_geojson(conn)["features"][0]
    assert feature["properties"]["color"] == "#888888"
