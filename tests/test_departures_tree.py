from datetime import datetime

from hvv_map.departures_tree import (
    OTHER_MODE,
    build_station_tree,
    parse_anchor,
    refined_model,
    ubahn_series,
    vehicle_numbers_text,
    vehicles_text,
)

ANCHOR = datetime(2026, 10, 16, 4, 0)


def _dep(name, short_info, station, offset, service_id="1", **extra):
    return {
        "line": {
            "id": f"id-{name}",
            "name": name,
            "type": {"shortInfo": short_info},
            "direction": "Somewhere",
        },
        "station": {"id": f"st-{station}", "combinedName": station},
        "timeOffset": offset,
        "serviceId": service_id,
        "directionId": 1,
        **extra,
    }


def test_parse_anchor_reads_date_and_time():
    assert parse_anchor({"date": "2026-10-16", "time": "04:00:00"}) == ANCHOR


def test_parse_anchor_none_when_missing_or_malformed():
    assert parse_anchor(None) is None
    assert parse_anchor({"date": "nope", "time": "04:00:00"}) is None


def test_station_tree_groups_mode_line_station():
    departures = [
        _dep("U1", "U", "Ohlstedt", 5),
        _dep("U1", "U", "Volksdorf", 9),
        _dep("S1", "S", "Ohlsdorf", 7),
    ]
    tree = build_station_tree(departures, ANCHOR)
    assert [m["mode"] for m in tree["modes"]] == ["U", "S"]
    u1 = tree["modes"][0]["lines"][0]
    assert (u1["name"], u1["station_count"], u1["departure_count"]) == ("U1", 2, 2)


def test_station_tree_computes_clock_times_and_next_day():
    departures = [_dep("U1", "U", "Ohlstedt", 5), _dep("U1", "U", "Ohlstedt", 1430)]
    stop = build_station_tree(departures, ANCHOR)["modes"][0]["lines"][0]["stations"][0]
    assert [(d["time"], d["next_day"]) for d in stop["departures"]] == [
        ("04:05", False),
        ("03:50", True),
    ]


def test_station_tree_sorts_departures_by_offset_and_lines_naturally():
    departures = [
        _dep("U10", "U", "A", 30),
        _dep("U2", "U", "A", 20),
        _dep("U2", "U", "A", 10),
    ]
    lines = build_station_tree(departures, ANCHOR)["modes"][0]["lines"]
    assert [ln["name"] for ln in lines] == ["U2", "U10"]
    assert [d["offset"] for d in lines[0]["stations"][0]["departures"]] == [10, 20]


def test_station_tree_without_anchor_has_no_clock_time():
    stop = build_station_tree([_dep("U1", "U", "A", 5)], None)["modes"][0]["lines"][0][
        "stations"
    ][0]
    assert stop["departures"][0]["time"] is None


def test_station_tree_prefers_realtime_platform():
    departures = [_dep("U1", "U", "A", 5, platform="1", realtimePlatform="2")]
    stop = build_station_tree(departures, ANCHOR)["modes"][0]["lines"][0]["stations"][0]
    assert stop["departures"][0]["platform"] == "2"
    assert stop["departures"][0]["planned_platform"] == "1"
    assert stop["departures"][0]["realtime_platform"] == "2"


def test_station_tree_puts_unmapped_lines_into_other_mode_last():
    departures = [_dep("175", "Bus", "A", 5), _dep("U1", "U", "A", 5)]
    modes = [m["mode"] for m in build_station_tree(departures, ANCHOR)["modes"]]
    assert modes == ["U", OTHER_MODE]


def test_vehicles_text_formats_number_and_falls_back_to_id():
    assert vehicles_text([{"id": "9001", "number": "4711"}, {"id": "7"}]) == (
        "4711, (id 7)"
    )
    assert vehicles_text(None) == ""
    assert vehicles_text([{}]) == ""


def test_station_tree_carries_vehicles():
    vehicles = [{"id": "9001", "number": "4711"}]
    dep = _dep("U1", "U", "Ochsenzoll", 2, vehicles=vehicles)
    station = build_station_tree([dep], ANCHOR)["modes"][0]["lines"][0]["stations"][0]
    assert station["departures"][0]["vehicles"] == "4711"


def test_station_tree_carries_line_model():
    dep = _dep("U1", "U", "Ochsenzoll", 2)
    dep["line"]["type"]["model"] = "DT5"
    station = build_station_tree([dep], ANCHOR)["modes"][0]["lines"][0]["stations"][0]
    assert station["departures"][0]["model"] == "DT5"


def test_station_tree_carries_delay():
    dep = _dep("S1", "S", "Altona", 4, delay=120)
    station = build_station_tree([dep], ANCHOR)["modes"][0]["lines"][0]["stations"][0]
    assert station["departures"][0]["delay"] == 120


def test_ubahn_series_by_unit_number():
    assert ubahn_series("101") == "DT4.1"
    assert ubahn_series(226) == "DT4.6"
    assert ubahn_series("417") == "DT5.1"
    assert ubahn_series("437") == "DT5.2"
    assert ubahn_series("4711") is None
    assert ubahn_series(None) is None


def test_vehicles_text_adds_series_for_ubahn_only():
    vehicles = [{"id": "9", "number": "355"}]
    assert vehicles_text(vehicles, ubahn=True) == "DT5.1 (355)"
    assert vehicles_text(vehicles) == "355"


def test_refined_model_prefers_vehicle_numbers_on_ubahn():
    line = {"type": {"shortInfo": "U", "model": "DT4"}}
    three_dt5 = [{"number": n} for n in ("417", "355", "437")]
    assert refined_model(line, three_dt5) == "DT5"
    assert refined_model(line, [{"number": "129"}, {"number": "420"}]) == "DT4/DT5"
    assert refined_model(line, []) == "DT4"
    bus = {"type": {"shortInfo": "Bus", "model": "Solobus"}}
    assert refined_model(bus, [{"number": "417"}]) == "Solobus"


def test_vehicles_text_keeps_the_reported_unit_order():
    vehicles = [{"number": "362", "id": "b"}, {"number": "329", "id": "a"}]

    assert vehicles_text(vehicles, ubahn=True) == "DT5.1 (362), DT5.1 (329)"
    assert vehicles_text(vehicles[::-1], ubahn=True) == "DT5.1 (329), DT5.1 (362)"
    assert vehicles_text([{"id": "x9"}, {"number": "4711"}]) == "(id x9), 4711"


def test_vehicle_numbers_text_lists_numbers_with_id_fallback():
    vehicles = [{"id": "9001", "number": "4711"}, {"id": "7"}, {}]
    assert vehicle_numbers_text(vehicles) == "4711, id 7"
    assert vehicle_numbers_text(None) == ""
