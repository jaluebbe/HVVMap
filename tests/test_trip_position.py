from datetime import datetime, timedelta

from hvv_map.trip_position import (
    AFTER,
    AT_STOP,
    BEFORE,
    BETWEEN,
    dwell_seconds,
    locate,
    stops_from_row,
)

T0 = datetime(2026, 10, 17, 9, 0)


def at(minutes, seconds=0):
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def stop(arr=None, dep=None, delay=None):
    return {
        "name": "x",
        "arr": at(arr) if arr is not None else None,
        "dep": at(dep) if dep is not None else None,
        "delay": delay,
    }


# A -(dep 0)-> B (arr 4, dep 5) -> C (arr 9)
TRIP = [stop(dep=0), stop(arr=4, dep=5), stop(arr=9)]


def test_dwell_uses_plan_else_default_and_ferry_minimum():
    assert dwell_seconds(at(4), at(5), False) == 60
    assert dwell_seconds(at(5), at(5), False) == 30
    assert dwell_seconds(None, at(5), False) == 30
    assert dwell_seconds(at(5), at(5), True) == 60
    assert dwell_seconds(at(4), at(7), True) == 180


def test_before_and_after_the_trip():
    assert locate(TRIP, at(-5)).state == BEFORE
    assert locate(TRIP, at(30)).state == AFTER


def test_at_stop_during_the_dwell():
    position = locate(TRIP, at(4, 30))
    assert (position.state, position.index) == (AT_STOP, 1)
    assert locate(TRIP, at(5)).index == 1


def test_between_stops_reports_progress_on_the_segment():
    position = locate(TRIP, at(2, 30))
    assert position.state == BETWEEN
    assert (position.index, position.end_index) == (0, 1)
    # departs 0:00, arrives 4:00 -> 2:30 is 62.5 %
    assert round(position.progress, 3) == 0.625


def test_zero_planned_dwell_places_arrival_30_seconds_before_departure():
    trip = [stop(dep=0), stop(arr=5, dep=5), stop(arr=9)]
    assert locate(trip, at(4, 20)).state == BETWEEN
    assert locate(trip, at(4, 40)).state == AT_STOP


def test_ferry_dwells_at_least_a_minute():
    trip = [stop(dep=0), stop(arr=5, dep=5), stop(arr=9)]
    assert locate(trip, at(4, 20), ferry=True).state == AT_STOP


def test_delay_shifts_times_and_carries_forward():
    trip = [stop(dep=0, delay=120), stop(arr=4, dep=5), stop(arr=9, delay=0)]
    # B inherits the 120 s delay: dwell 6:00 to 7:00
    assert locate(trip, at(4, 30)).state == BETWEEN
    assert locate(trip, at(6, 30)).index == 1
    # C is on time again, so the last segment is shortened
    assert locate(trip, at(8, 30)).state == BETWEEN


def test_terminus_stays_for_the_dwell_then_trip_is_over():
    assert locate(TRIP, at(9, 20)).index == 2
    assert locate(TRIP, at(9, 40)).state == AFTER


def test_trip_without_times_has_no_position():
    assert locate([], at(0)) is None
    assert locate([stop(dep=0), stop(), stop(arr=9)], at(0)) is None


def test_stops_without_times_after_the_last_known_one_are_ignored():
    trip = [stop(dep=0), stop(arr=4, dep=5), stop(), stop()]
    assert locate(trip, at(2)).state == BETWEEN
    assert locate(trip, at(30)).state == AFTER


def test_stops_from_row_resolves_times_across_midnight():
    row = {
        "stops": [
            {
                "station": "A",
                "arr": None,
                "dep": "23:58",
                "at": "2026-10-17T23:58",
                "delay": 0,
            },
            {
                "station": "B",
                "arr": "00:03",
                "dep": "00:03",
                "at": "2026-10-18T00:03",
                "delay": 60,
            },
            {
                "station": "C",
                "arr": None,
                "dep": None,
                "at": "2026-10-18T00:08",
                "delay": None,
            },
        ]
    }

    stops = stops_from_row(row)

    assert stops[0]["dep"] == datetime(2026, 10, 17, 23, 58)
    assert stops[1]["arr"] == datetime(2026, 10, 18, 0, 3)
    assert stops[2]["dep"] == datetime(2026, 10, 18, 0, 8)
    assert stops[1]["delay"] == 60
