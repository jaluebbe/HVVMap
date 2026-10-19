from datetime import date, datetime

from hvv_map.plan_debug import compare, night_trips, night_window


def _entry(line, station, offset, destination="X"):
    return {
        "line": {"name": line},
        "destination": destination,
        "stops": [{"timeOffset": offset, "station": {"name": station}}],
    }


def test_night_window_covers_midnight_to_four_of_the_next_day():
    start, end = night_window(date(2026, 10, 18))
    assert start == datetime(2026, 10, 19, 0, 0)
    assert end == datetime(2026, 10, 19, 4, 0)


def test_night_trips_keeps_only_trips_starting_in_the_window():
    anchor = datetime(2026, 10, 18, 23, 0)
    by_service = {
        "1": _entry("S1", "Altona", 33),  # 23:33, before the window
        "2": _entry("S1", "Altona", 80),  # 00:20
        "3": _entry("S1", "Altona", 300),  # 04:00, after the window
    }
    trips = night_trips(by_service, anchor, night_window(date(2026, 10, 18)))
    assert list(trips) == [("S1", "Altona", "00:20")]


def test_compare_lists_trips_unique_to_one_night():
    nights = {
        "Sa": {("RE70", "Kiel Hbf", "01:05"): {"destination": "Hamburg Hbf"}},
        "So": {},
    }
    text = compare(nights)
    assert "nur in Sa: 1" in text
    assert "RE70" in text and "nur in So: 0" in text
