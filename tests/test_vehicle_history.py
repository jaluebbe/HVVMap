from datetime import datetime

from hvv_map.vehicle_history import format_sighting, matches, parse_since

SIGHTING = {
    "vehicle": "353",
    "with": ["367"],
    "line": "U3",
    "station": "Berliner Tor",
    "destination": "Rathaus",
    "planned": "2026-10-20T17:26",
    "delay": 0,
    "platform": "Gleis 4",
    "service_id": 21599,
    "source": "departureList",
    "seen_at": 1792509023,
}


def test_matches_combines_vehicle_station_and_line_filters():
    assert matches(SIGHTING, set(), None, None)
    assert matches(SIGHTING, {"353"}, "berliner", "u3")
    assert not matches(SIGHTING, {"301"}, None, None)
    assert not matches(SIGHTING, set(), "Barmbek", None)
    assert not matches(SIGHTING, set(), None, "U1")


def test_matches_tolerates_sightings_without_station():
    assert not matches({**SIGHTING, "station": None}, set(), "Tor", None)
    assert matches({**SIGHTING, "station": None}, set(), None, None)


def test_format_shows_time_vehicle_station_and_partners():
    text = format_sighting(SIGHTING)

    assert "353" in text and "Berliner Tor" in text and "17:26 -> Rathaus" in text
    assert "mit 367" in text and "ID 21599" in text
    assert " list " in text
    assert " course " in format_sighting({**SIGHTING, "source": "departureCourse"})


def test_since_is_todays_wall_clock_time_as_a_stream_id_in_milliseconds():
    now = datetime(2026, 10, 20, 17, 30)

    # 2026-10-20 16:30 Berlin (UTC+2) = 14:30 UTC
    assert parse_since("16:30", now) == 1792506600000
