from hvv_map.gti_shapes import open_db, store_segment
from hvv_map.gtilive_debug import build_debug_geojson
from hvv_map.stations import StationInfo


def _station(sid, name, lon, lat):
    return StationInfo(
        id=sid, name=name, city="Hamburg", lon=lon, lat=lat, combined_name=name
    )


STATIONS = {
    "S1": _station("S1", "Ohlstedt", 10.1, 53.7),
    "S2": _station("S2", "Volksdorf", 10.15, 53.68),
    "S3": _station("S3", "Buchenkamp", 10.16, 53.67),
}
SUBLINE = [{"id": "S1", "name": "Ohlstedt"}, {"id": "S2", "name": "Volksdorf"}]


def _entry(name, short_info, subline, stop_ids=()):
    return {
        "line": {"name": name, "id": f"id-{name}", "type": {"shortInfo": short_info}},
        "stops": [{"station": {"id": sid}} for sid in stop_ids],
        "subline": subline,
    }


def _of_kind(result, kind):
    return [f for f in result["features"] if f["properties"]["kind"] == kind]


def test_line_feature_counts_services_and_flags_straight_geometry():
    by_service = {"1": _entry("U1", "U", SUBLINE), "2": _entry("U1", "U", SUBLINE)}
    result = build_debug_geojson(by_service, STATIONS)
    (line,) = _of_kind(result, "line")
    props = line["properties"]
    assert props["services"] == 2
    assert (props["hops"], props["real_hops"]) == (1, 0)
    assert props["stops"] == ["Ohlstedt", "Volksdorf"]
    assert props["subline_key"] == "id-U1|S1|S2"


def test_line_feature_counts_real_hops_from_shapes_db():
    conn = open_db(":memory:")
    store_segment(conn, "S1", "S2", [[10.1, 53.7], [10.12, 53.69], [10.15, 53.68]])
    by_service = {"1": _entry("U1", "U", SUBLINE)}
    (line,) = _of_kind(build_debug_geojson(by_service, STATIONS, conn), "line")
    assert line["properties"]["real_hops"] == 1


def test_line_color_known_flag_and_fallback():
    by_service = {
        "1": _entry("U1", "U", SUBLINE),
        "2": _entry("S99", "S", SUBLINE),
    }
    lines = {
        f["properties"]["lines"][0]: f["properties"]
        for f in _of_kind(build_debug_geojson(by_service, STATIONS), "line")
    }
    assert lines["U1"]["color_known"] is True
    assert (lines["S99"]["color"], lines["S99"]["color_known"]) == ("#888888", False)


def test_summary_counts_unresolved_services_per_line():
    by_service = {
        "1": _entry("U1", "U", SUBLINE),
        "2": _entry("RE8", "RE", None),
        "3": _entry("RE8", "RE", None),
        "4": _entry("175", "Bus", None),  # unrelated bus, not counted
    }
    debug = build_debug_geojson(by_service, STATIONS)["debug"]
    assert (debug["services"], debug["resolved"], debug["unresolved"]) == (3, 1, 2)
    assert debug["unresolved_by_line"] == [{"line": "RE8", "mode": "R", "count": 2}]


def test_orphan_stop_only_for_stations_missing_from_resolved_sublines():
    by_service = {
        "1": _entry("U1", "U", SUBLINE),
        "2": _entry("RE8", "RE", None, stop_ids=["S2", "S3"]),
    }
    stops = {
        f["properties"]["id"]: f["properties"]
        for f in _of_kind(build_debug_geojson(by_service, STATIONS), "stop")
    }
    assert stops["S2"]["resolved"] is True
    assert stops["S3"]["resolved"] is False
    assert stops["S3"]["lines"] == ["RE8"]


def test_stations_without_coordinates_are_counted_not_drawn():
    by_service = {"1": _entry("RE8", "RE", None, stop_ids=["unknown"])}
    result = build_debug_geojson(by_service, STATIONS)
    assert _of_kind(result, "stop") == []
    assert result["debug"]["missing_stations"] == [
        {"id": "unknown", "name": "unknown", "lines": ["RE8"]}
    ]


def test_missing_station_reports_name_and_lines_from_the_subline():
    subline = [{"id": "S1", "name": "Ohlstedt"}, {"id": "gone", "name": "Elbbrücken"}]
    by_service = {"1": _entry("S3", "S", subline)}
    missing = build_debug_geojson(by_service, STATIONS)["debug"]["missing_stations"]
    assert missing == [{"id": "gone", "name": "Elbbrücken", "lines": ["S3"]}]


def test_stop_lists_every_mode_so_the_page_can_filter_on_any_of_them():
    by_service = {"1": _entry("U1", "U", SUBLINE), "2": _entry("S1", "S", SUBLINE)}
    stops = _of_kind(build_debug_geojson(by_service, STATIONS), "stop")
    assert all(f["properties"]["modes"] == ["S", "U"] for f in stops)


def test_line_is_split_into_runs_of_real_and_straight_hops():
    conn = open_db(":memory:")
    store_segment(conn, "S1", "S2", [[10.1, 53.7], [10.12, 53.69], [10.15, 53.68]])
    subline = [*SUBLINE, {"id": "S3", "name": "Buchenkamp"}]
    by_service = {"1": _entry("U1", "U", subline)}
    runs = _of_kind(build_debug_geojson(by_service, STATIONS, conn), "line")
    assert [f["properties"]["straight"] for f in runs] == [False, True]
    assert [len(f["geometry"]["coordinates"]) for f in runs] == [3, 2]
    assert all(f["properties"]["hops"] == 2 for f in runs)
    assert all(f["properties"]["real_hops"] == 1 for f in runs)


def test_line_without_any_segment_is_one_straight_run():
    by_service = {"1": _entry("U1", "U", SUBLINE)}
    (line,) = _of_kind(build_debug_geojson(by_service, STATIONS), "line")
    assert line["properties"]["straight"] is True


def test_stored_two_point_segment_counts_as_straight():
    # gti_shapes stores a straight line when departureCourse has no track.
    conn = open_db(":memory:")
    store_segment(conn, "S1", "S2", [[10.1, 53.7], [10.15, 53.68]])
    by_service = {"1": _entry("A2-SEV", "Bus", SUBLINE)}
    (line,) = _of_kind(build_debug_geojson(by_service, STATIONS, conn), "line")
    assert line["properties"]["straight"] is True
    assert line["properties"]["real_hops"] == 0
