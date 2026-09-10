from datetime import date

import pytest

from hvv_map.gtfs_schedule import (
    active_service_ids,
    find_current_segment,
    head_position,
    load_schedule,
    mode_for_trip,
)


def _write_csv(path, fieldnames, rows):
    import csv

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def gtfs_dir(tmp_path):
    _write_csv(
        tmp_path / "routes.txt",
        ["route_id", "route_short_name", "route_type"],
        [
            {"route_id": "R_U1", "route_short_name": "U1", "route_type": "402"},
            {"route_id": "R_BUS", "route_short_name": "123", "route_type": "3"},
        ],
    )
    _write_csv(
        tmp_path / "stops.txt",
        ["stop_id", "stop_name", "stop_lat", "stop_lon"],
        [
            {
                "stop_id": "A",
                "stop_name": "Alpha",
                "stop_lat": "53.5",
                "stop_lon": "10.0",
            },
            {
                "stop_id": "B",
                "stop_name": "Beta",
                "stop_lat": "53.6",
                "stop_lon": "10.1",
            },
        ],
    )
    _write_csv(
        tmp_path / "trips.txt",
        ["trip_id", "route_id", "service_id", "trip_headsign"],
        [
            {
                "trip_id": "T1",
                "route_id": "R_U1",
                "service_id": "WEEKDAYS",
                "trip_headsign": "Beta",
            },
        ],
    )
    _write_csv(
        tmp_path / "stop_times.txt",
        ["trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"],
        [
            {
                "trip_id": "T1",
                "stop_id": "A",
                "stop_sequence": "1",
                "arrival_time": "08:00:00",
                "departure_time": "08:00:00",
            },
            {
                "trip_id": "T1",
                "stop_id": "B",
                "stop_sequence": "2",
                "arrival_time": "08:02:00",
                "departure_time": "08:02:00",
            },
        ],
    )
    _write_csv(
        tmp_path / "calendar.txt",
        [
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ],
        [
            {
                "service_id": "WEEKDAYS",
                "monday": "1",
                "tuesday": "1",
                "wednesday": "1",
                "thursday": "1",
                "friday": "1",
                "saturday": "0",
                "sunday": "0",
                "start_date": "20200101",
                "end_date": "20301231",
            },
        ],
    )
    _write_csv(
        tmp_path / "calendar_dates.txt",
        ["service_id", "date", "exception_type"],
        [
            {
                "service_id": "WEEKDAYS",
                "date": "20250101",
                "exception_type": "2",
            },  # removed (holiday)
            {
                "service_id": "SPECIAL",
                "date": "20250706",
                "exception_type": "1",
            },  # added, no calendar.txt row
        ],
    )
    return str(tmp_path)


def test_load_schedule_filters_routes_of_interest(gtfs_dir):
    schedule = load_schedule(gtfs_dir)
    assert "R_U1" in schedule.routes
    assert "R_BUS" not in schedule.routes  # unrelated bus route excluded


def test_load_schedule_only_keeps_trips_of_interesting_routes(gtfs_dir):
    schedule = load_schedule(gtfs_dir)
    assert set(schedule.trips) == {"T1"}


def test_mode_for_trip_maps_route_type_to_mode(gtfs_dir):
    schedule = load_schedule(gtfs_dir)
    assert mode_for_trip(schedule, schedule.trips["T1"]) == "U"


def test_active_service_ids_respects_weekday_and_date_range(gtfs_dir):
    schedule = load_schedule(gtfs_dir)
    monday = date(2025, 6, 2)
    saturday = date(2025, 6, 7)
    assert "WEEKDAYS" in active_service_ids(schedule, monday)
    assert "WEEKDAYS" not in active_service_ids(schedule, saturday)


def test_active_service_ids_exception_removes_service(gtfs_dir):
    schedule = load_schedule(gtfs_dir)
    new_years_day = date(2025, 1, 1)  # a Wednesday, but removed via calendar_dates
    assert "WEEKDAYS" not in active_service_ids(schedule, new_years_day)


def test_active_service_ids_exception_adds_service_without_calendar_row(gtfs_dir):
    schedule = load_schedule(gtfs_dir)
    special_day = date(2025, 7, 6)  # a Sunday, added purely via calendar_dates
    assert "SPECIAL" in active_service_ids(schedule, special_day)


def test_find_current_segment_returns_matching_pair():
    rows = [(1, "A", 100, 100), (2, "B", 200, 200), (3, "C", 300, 300)]
    assert find_current_segment(rows, 150) == ((1, "A", 100, 100), (2, "B", 200, 200))


def test_find_current_segment_none_before_first_departure():
    rows = [(1, "A", 100, 100), (2, "B", 200, 200)]
    assert find_current_segment(rows, 50) is None


def test_find_current_segment_none_after_last_arrival():
    rows = [(1, "A", 100, 100), (2, "B", 200, 200)]
    assert find_current_segment(rows, 250) is None


def test_head_position_midway():
    coords = [(0.0, 0.0), (0.0, 2.0)]
    progress, head = head_position(coords, actual_start=0, actual_end=100, now_ts=50)
    assert progress == pytest.approx(0.5)
    assert head[1] == pytest.approx(1.0, abs=0.01)


def test_head_position_clamps_before_start():
    coords = [(0.0, 0.0), (0.0, 2.0)]
    progress, head = head_position(coords, actual_start=100, actual_end=200, now_ts=0)
    assert progress == 0.0
    assert head == coords[0]


def test_head_position_clamps_after_end():
    coords = [(0.0, 0.0), (0.0, 2.0)]
    progress, head = head_position(coords, actual_start=0, actual_end=100, now_ts=500)
    assert progress == 1.0
    assert head == coords[1]


def test_head_position_none_for_single_point():
    assert (
        head_position([(0.0, 0.0)], actual_start=0, actual_end=100, now_ts=50) is None
    )
