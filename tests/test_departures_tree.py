from datetime import datetime

from hvv_map.departures_tree import (
    OTHER_MODE,
    build_station_tree,
    build_trip_tree,
    parse_anchor,
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


def test_station_tree_puts_unmapped_lines_into_other_mode_last():
    departures = [_dep("175", "Bus", "A", 5), _dep("U1", "U", "A", 5)]
    modes = [m["mode"] for m in build_station_tree(departures, ANCHOR)["modes"]]
    assert modes == ["U", OTHER_MODE]


def _service(name, short_info, stops, subline=None):
    return {
        "line": {
            "id": f"id-{name}",
            "name": name,
            "origin": stops[0][0],
            "type": {"shortInfo": short_info},
        },
        "directionId": 1,
        "destination": stops[-1][0],
        "subline": subline,
        "stops": [
            {"timeOffset": offset, "station": {"id": s, "combinedName": s}}
            for s, offset in stops
        ],
    }


def test_trip_tree_counts_resolved_sublines_per_line():
    by_service = {
        "1": _service("RE8", "RE", [("Hbf", 10), ("Strand", 70)], [{"id": "x"}] * 2),
        "2": _service("RE8", "RE", [("Hbf", 20), ("Strand", 80)]),
    }
    line = build_trip_tree(by_service, ANCHOR)["modes"][0]["lines"][0]
    assert (line["trip_count"], line["resolved_count"]) == (2, 1)
    assert [t["subline_stop_count"] for t in line["trips"]] == [2, None]


def test_trip_tree_trip_details():
    by_service = {"7": _service("U1", "U", [("A", 5), ("B", 9)])}
    trip = build_trip_tree(by_service, ANCHOR)["modes"][0]["lines"][0]["trips"][0]
    assert trip["service_id"] == "7"
    assert (trip["origin"], trip["destination"], trip["stop_count"]) == ("A", "B", 2)
    assert [s["time"] for s in trip["stops"]] == ["04:05", "04:09"]


def test_trip_tree_orders_trips_by_first_stop():
    by_service = {
        "late": _service("U1", "U", [("A", 50), ("B", 55)]),
        "early": _service("U1", "U", [("A", 5), ("B", 9)]),
    }
    trips = build_trip_tree(by_service, ANCHOR)["modes"][0]["lines"][0]["trips"]
    assert [t["service_id"] for t in trips] == ["early", "late"]
