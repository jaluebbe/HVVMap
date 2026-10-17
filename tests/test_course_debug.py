from hvv_map.course_debug import course_request, format_course, format_tracks

LIVE = {
    "anchor": {"date": "2026-10-17", "time": "14:56:00"},
    "departures": [
        {
            "serviceId": 365953386,
            "timeOffset": 3,
            "line": {"dlid": "de:hvv:S1:"},
            "station": {"id": "Master:1", "combinedName": "Berliner Tor"},
        },
        {
            "serviceId": 365953386,
            "timeOffset": 9,
            "line": {"dlid": "de:hvv:S1:"},
            "station": {"id": "Master:2", "combinedName": "Barmbek"},
        },
    ],
}


def test_request_uses_first_matching_departure_with_berlin_time():
    request = course_request(LIVE, 365953386)

    assert request["station"]["name"] == "Berliner Tor"
    assert request["time"] == "2026-10-17T14:59:00.000+0200"
    assert request["lineId"] == "de:hvv:S1:" and request["serviceId"] == 365953386


def test_request_can_pick_a_station():
    assert course_request(LIVE, 365953386, "Master:2")["station"]["name"] == "Barmbek"


def test_request_is_none_for_unknown_service_id():
    assert course_request(LIVE, 1) is None


def test_format_course_lists_one_line_per_station():
    stops = [
        {
            "name": "A",
            "arr": None,
            "dep": "2026-10-17T09:02",
            "dep_delay": 60,
            "arr_delay": None,
            "platform": "Gleis 1",
        },
        {
            "name": "B",
            "arr": "2026-10-17T09:05",
            "dep": None,
            "dep_delay": None,
            "arr_delay": 0,
            "platform": None,
        },
    ]
    text = format_course(stops).splitlines()
    assert len(text) == 3 and "09:02" in text[1] and "09:05" in text[2]


def test_format_tracks_flags_elements_without_geometry():
    response = {
        "courseElements": [
            {
                "fromStation": {"name": "A"},
                "toStation": {"name": "B"},
                "path": {"track": [{"x": 1, "y": 2}, {"x": 2, "y": 3}]},
            },
            {"fromStation": {"name": "B"}, "toStation": {"name": "C"}},
        ]
    }

    assert format_tracks(response).splitlines() == [
        "A -> B: 2 Punkte",
        "B -> C: keine Geometrie (Luftlinie)",
    ]
