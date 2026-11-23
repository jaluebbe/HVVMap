import json
from datetime import datetime

import pytest

from hvv_map import train_numbers
from hvv_map.trips import alias_course_key, build_trip_table


@pytest.fixture
def tables(tmp_path, monkeypatch):
    monkeypatch.setattr(train_numbers, "NUMBERS_DIR", str(tmp_path))
    train_numbers._cache.clear()

    def write(day, stops):
        path = tmp_path / f"train_numbers_{day}.json"
        path.write_text(json.dumps({"stops": stops}))

    return write


def at(hhmm, day=23):
    return datetime(2026, 11, day, *map(int, hhmm.split(":")))


def test_number_listed_at_every_stop(tables):
    tables("20261123", {
        "S7|de:02000:10952|2026-11-23T05:17": [["47027", "de:02000:49950"], ["47028", "de:02000:10950"]],
        "S7|de:02000:10950|2026-11-23T05:20": [["47028", "de:02000:8002548"]],
    })  # fmt: skip
    stops = [("de:02000:10952", at("05:17")), ("de:02000:10950", at("05:20"))]
    assert train_numbers.lookup("S7", stops) == "47028"


def test_coupled_trains_and_a_single_ambiguous_stop(tables):
    tables("20261123", {
        "RE7|de:02000:10950|2026-11-23T10:21": [["11163", "x"], ["11263", "x"]],
        "RE7|de:02000:10951|2026-11-23T10:25": [["11163", "y"], ["11263", "y"]],
    })  # fmt: skip
    both = [("de:02000:10950", at("10:21")), ("de:02000:10951", at("10:25"))]
    assert train_numbers.lookup("RE7", both) == "11163/11263"
    assert train_numbers.lookup("RE7", both[:1]) == "11163/11263"  # same next station


def test_single_stop_with_trains_to_different_next_stations(tables):
    tables("20261123", {"S7|de:02000:10952|2026-11-23T05:17": [["47027", "a"], ["47028", "b"]]})
    assert train_numbers.lookup("S7", [("de:02000:10952", at("05:17"))]) == ""


def test_after_midnight_from_the_previous_service_day(tables):
    tables("20261122", {"S1|de:02000:1|2026-11-23T00:33": [["41021", "x"]]})
    assert train_numbers.lookup("S1", [("de:02000:1:1:1", at("00:33"))]) == "41021"


def test_without_tables(monkeypatch):
    monkeypatch.setattr(train_numbers, "NUMBERS_DIR", "")
    assert train_numbers.lookup("S1", [("de:02000:1", at("08:00"))]) == ""


def test_trip_table_rows_of_s_bahn_get_the_number(tables):
    tables("20261017", {"S1|de:02000:11|2026-10-17T09:02": [["41021", "de:02000:12"]]})
    plan = {
        "1": {
            "line": {"name": "S1", "id": "L:S1", "type": {"shortInfo": "S"}},
            "directionId": 1,
            "destination": "Wedel",
            "stops": [
                {"timeOffset": 302, "station": {"id": "Master:11", "globalId": "de:02000:11", "name": "A"}},
            ],
        }
    }  # fmt: skip
    live = {"anchor": {"date": "2026-10-17", "time": "09:00:00"}, "departures": []}
    table = build_trip_table(plan, datetime(2026, 10, 17, 4, 0), live)
    assert table["trips"][0]["train_number"] == "41021"


def test_course_stops_tell_apart_trains_leaving_an_anchor_together(tables):
    tables("20261017", {
        "A3|de:01056:99960|2026-10-17T09:02": [["268903", "de:01056:37994"], ["269912", "de:01056:8007069"]],
        "A3|de:01056:8007069|2026-10-17T09:03": [["269912", "de:01056:99962"]],
    })  # fmt: skip
    plan = {
        "1": {
            "line": {"name": "A3", "id": "L:A3", "type": {"shortInfo": "A"}},
            "directionId": 1,
            "destination": "Elmshorn",
            "stops": [
                {"timeOffset": 302, "station": {"id": "Master:1", "globalId": "de:01056:99960", "name": "Barmstedt"}},
            ],
        }
    }  # fmt: skip
    fields = {"arr": None, "arr_delay": None, "dep_delay": 0, "platform": None, "cancelled": False}
    course = {
        "stops": [
            {"id": "Master:1", "global_id": "de:01056:99960", "name": "Barmstedt", "dep": "2026-10-17T09:02", **fields},
            {"id": "Master:2", "global_id": "de:01056:8007069", "name": "B", "dep": "2026-10-17T09:03", **fields},
        ]
    }  # fmt: skip
    dep = {
        "line": {"name": "A3", "type": {"shortInfo": "A"}, "direction": "Elmshorn"},
        "station": {"id": "Master:1", "name": "Barmstedt"},
        "timeOffset": 2,
        "directionId": 1,
        "serviceId": 900,
    }
    live = {"anchor": {"date": "2026-10-17", "time": "09:00:00"}, "departures": [dep]}
    without = build_trip_table(plan, datetime(2026, 10, 17, 4, 0), live)
    assert without["trips"][0]["train_number"] == ""
    courses = {alias_course_key("900"): course}
    table = build_trip_table(plan, datetime(2026, 10, 17, 4, 0), live, courses)
    assert table["trips"][0]["train_number"] == "269912"
