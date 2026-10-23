from hvv_map.gti_shapes import open_db, store_segment
from hvv_map.gtilive_geojson import (
    _subline_coordinates,
    build_lines_geojson,
    build_stops_geojson,
    mode_for_line,
)
from hvv_map.stations import StationInfo

STATIONS = {
    "Master:1": StationInfo(
        id="Master:1",
        name="Ohlstedt",
        city="Hamburg",
        lon=10.1,
        lat=53.7,
        combined_name="Ohlstedt",
    ),  # fmt: skip
    "Master:2": StationInfo(
        id="Master:2",
        name="Volksdorf",
        city="Hamburg",
        lon=10.15,
        lat=53.68,
        combined_name="Volksdorf",
    ),  # fmt: skip
    "SCM:9057819": StationInfo(
        id="SCM:9057819",
        name="Hbf",
        city="Lübeck",
        lon=10.67,
        lat=53.87,
        combined_name="Lübeck Hbf",
    ),  # fmt: skip
}


def _entry(line_name, short_info, subline, line_id="x"):
    return {
        "line": {"name": line_name, "id": line_id, "type": {"shortInfo": short_info}},
        "directionId": 1,
        "stops": [],
        "destination": "",
        "subline": subline,
    }


SUBLINE = [
    {"id": "Master:1", "name": "Ohlstedt"},
    {"id": "Master:2", "name": "Volksdorf"},
]


def test_mode_for_line_by_short_info():
    cases = {
        "U": "U",
        "S": "S",
        "A": "AKN",
        "Schiff": "FERRY",
        "RB": "R",
        "RE": "R",
    }
    for short_info, expected_mode in cases.items():
        assert (
            mode_for_line({"name": "x", "type": {"shortInfo": short_info}})
            == expected_mode
        )


def test_mode_for_line_replacement_bus_by_name():
    line = {"name": "S1-SEV", "type": {"shortInfo": "Bus"}}
    assert mode_for_line(line) == "S"


def test_mode_for_line_unrelated_bus_returns_empty():
    line = {"name": "175", "type": {"shortInfo": "Bus"}}
    assert mode_for_line(line) == ""


def test_build_stops_geojson_one_feature_per_station():
    by_service = {"1": _entry("U1", "U", SUBLINE)}
    result = build_stops_geojson(by_service, STATIONS)
    assert len(result["features"]) == 2


def test_build_stops_geojson_geometry_and_properties():
    by_service = {"1": _entry("U1", "U", SUBLINE)}
    feature = build_stops_geojson(by_service, STATIONS)["features"][0]
    assert feature["geometry"] == {"type": "Point", "coordinates": [10.1, 53.7]}
    assert feature["properties"] == {
        "id": "Master:1",
        "name": "Ohlstedt",
        "modes": ["U"],
        "lines": ["U1"],
        "text": "Ohlstedt",
    }


def test_build_stops_geojson_labels_with_combined_name_not_name():
    # Regression: listStations splits some stations into name="Hbf",
    # city="Lübeck" - the label must use the recombined combined_name
    # ("Lübeck Hbf"), not the bare name.
    subline = [{"id": "SCM:9057819", "name": "Lübeck Hbf"}]
    by_service = {"1": _entry("RE8", "RB", subline)}
    feature = build_stops_geojson(by_service, STATIONS)["features"][0]
    assert feature["properties"]["name"] == "Lübeck Hbf"
    assert feature["properties"]["text"] == "Lübeck Hbf"


def test_build_stops_geojson_skips_unresolved_subline():
    by_service = {"1": _entry("U1", "U", None)}
    result = build_stops_geojson(by_service, STATIONS)
    assert result["features"] == []


def test_build_stops_geojson_skips_unrelated_bus():
    by_service = {"1": _entry("175", "Bus", SUBLINE)}
    result = build_stops_geojson(by_service, STATIONS)
    assert result["features"] == []


def test_build_stops_geojson_combines_modes_at_interchange():
    by_service = {
        "1": _entry("U1", "U", SUBLINE[:1]),
        "2": _entry("S1", "S", SUBLINE[:1]),
    }
    feature = build_stops_geojson(by_service, STATIONS)["features"][0]
    assert feature["properties"]["modes"] == ["S", "U"]
    assert feature["properties"]["lines"] == ["S1", "U1"]


def test_subline_coordinates_uses_real_segment_regardless_of_station_lookup():
    # A resolved segment carries its own coordinates - a missing "to"
    # station in the lookup dict doesn't matter once the segment is real.
    conn = open_db(":memory:")
    store_segment(conn, "Master:1", "Master:2", [[10.1, 53.7], [10.15, 53.68]])
    coords = _subline_coordinates(("Master:1", "Master:2"), {}, conn)
    assert coords == [[10.1, 53.7], [10.15, 53.68]]


def test_subline_coordinates_skips_hop_with_no_segment_and_no_coordinates():
    coords = _subline_coordinates(("Master:unknown1", "Master:unknown2"), {}, None)
    assert coords == []


def test_subline_coordinates_falls_back_to_station_coordinates_without_segment():
    coords = _subline_coordinates(("Master:1", "Master:2"), STATIONS, None)
    assert coords == [[10.1, 53.7], [10.15, 53.68]]


def test_build_lines_geojson_geometry_and_properties():
    by_service = {"1": _entry("U1", "U", SUBLINE)}
    feature = build_lines_geojson(by_service, STATIONS)["features"][0]
    assert feature["geometry"] == {
        "type": "LineString",
        "coordinates": [[10.1, 53.7], [10.15, 53.68]],
    }
    assert feature["properties"] == {
        "lines": ["U1"],
        "modes": ["U"],
        "color": "#006ab3",
        "straight": True,
    }


def test_build_lines_geojson_splits_real_track_from_straight_hops():
    stations = {
        **STATIONS,
        "Master:3": StationInfo(
            id="Master:3",
            name="Berne",
            city="Hamburg",
            lon=10.2,
            lat=53.66,
            combined_name="Berne",
        ),  # fmt: skip
    }
    conn = open_db(":memory:")
    track = [[10.1, 53.7], [10.12, 53.69], [10.15, 53.68]]
    store_segment(conn, "Master:1", "Master:2", track)
    subline = [*SUBLINE, {"id": "Master:3", "name": "Berne"}]
    by_service = {"1": _entry("U1", "U", subline)}

    features = build_lines_geojson(by_service, stations, conn)["features"]

    assert [f["properties"]["straight"] for f in features] == [False, True]
    assert features[0]["geometry"]["coordinates"] == track
    assert features[1]["geometry"]["coordinates"] == [[10.15, 53.68], [10.2, 53.66]]


def test_build_lines_geojson_dedupes_repeat_trips_on_the_same_route():
    by_service = {
        "1": _entry("U1", "U", SUBLINE),
        "2": _entry("U1", "U", SUBLINE),
    }
    result = build_lines_geojson(by_service, STATIONS)
    assert len(result["features"]) == 1


def test_build_lines_geojson_draws_partial_line_past_a_missing_station():
    # Regression: a declared station missing its own hvv:stations coordinate
    # (e.g. a retired platform id listLines' stationSequence still
    # references, but listStations no longer does - the real U3
    # Barmbek/Saarlandstraße case) must not blank out the whole line, only
    # the one hop that can't be placed.
    subline = SUBLINE + [{"id": "Master:unknown", "name": "?"}]
    by_service = {"1": _entry("U1", "U", subline)}
    feature = build_lines_geojson(by_service, STATIONS)["features"][0]
    assert feature["geometry"]["coordinates"] == [[10.1, 53.7], [10.15, 53.68]]


def test_build_lines_geojson_skips_line_with_no_placeable_stations():
    subline = [
        {"id": "Master:unknown1", "name": "?"},
        {"id": "Master:unknown2", "name": "?"},
    ]
    by_service = {"1": _entry("U1", "U", subline)}
    result = build_lines_geojson(by_service, STATIONS)
    assert result["features"] == []


def test_build_lines_geojson_skips_single_station_subline():
    by_service = {"1": _entry("U1", "U", SUBLINE[:1])}
    result = build_lines_geojson(by_service, STATIONS)
    assert result["features"] == []


def test_build_lines_geojson_skips_unresolved_mode():
    # Not a recognized replacement-bus name, so mode_for_line() returns "".
    by_service = {"1": _entry("Metrobus 5", "Bus", SUBLINE)}
    result = build_lines_geojson(by_service, STATIONS)
    assert result["features"] == []


def test_build_lines_geojson_uses_real_track_where_resolved(tmp_path):
    conn = open_db(str(tmp_path / "gti_shapes.db"))
    store_segment(
        conn,
        "Master:1",
        "Master:2",
        [[10.1, 53.7], [10.12, 53.69], [10.15, 53.68]],
    )
    by_service = {"1": _entry("U1", "U", SUBLINE)}
    feature = build_lines_geojson(by_service, STATIONS, conn)["features"][0]
    assert feature["geometry"]["coordinates"] == [
        [10.1, 53.7],
        [10.12, 53.69],
        [10.15, 53.68],
    ]


def test_build_lines_geojson_falls_back_to_straight_line_when_unresolved(tmp_path):
    conn = open_db(str(tmp_path / "gti_shapes.db"))
    by_service = {"1": _entry("U1", "U", SUBLINE)}
    feature = build_lines_geojson(by_service, STATIONS, conn)["features"][0]
    assert feature["geometry"]["coordinates"] == [[10.1, 53.7], [10.15, 53.68]]
