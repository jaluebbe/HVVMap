from datetime import datetime

from hvv_map.trips import build_trip_table

PLAN_ANCHOR = datetime(2026, 10, 17, 4, 0)
LIVE_ANCHOR = {"date": "2026-10-17", "time": "09:00:00"}
U_TYPE = {"shortInfo": "U", "model": "DT4"}


def _plan_entry(line, direction_id, stops, destination="Ziel"):
    """stops: [(station id, minutes after the plan anchor)]"""
    return {
        "line": {"name": line, "id": f"L:{line}", "type": U_TYPE},
        "directionId": direction_id,
        "destination": destination,
        "stops": [
            {
                "timeOffset": offset,
                "direction": destination,
                "station": {"id": sid, "name": sid},
                "platform": "Gleis 1",
            }
            for sid, offset in stops
        ],
    }


def _live(line, sid, offset, direction_id, service_id=900, **extra):
    return {
        "line": {"name": line, "type": U_TYPE, "direction": "Ziel"},
        "station": {"id": sid, "name": sid},
        "timeOffset": offset,
        "directionId": direction_id,
        "serviceId": service_id,
        **extra,
    }


# 09:02 is 302 minutes after the plan anchor
PLAN = {"1": _plan_entry("U1", 6, [("A", 302), ("B", 306)])}


def test_live_departure_matches_plan_stop_by_line_station_direction_and_minute():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6, delay=60)]}

    table = build_trip_table(PLAN, PLAN_ANCHOR, live)

    trip = table["trips"][0]
    assert trip["status"] == "bestätigt"
    assert trip["live_ids"] == ["900"] and trip["max_delay"] == 60
    assert trip["stops"][0]["match"]["quality"] == "exakt"
    assert table["summary"]["live_matched"] == 1
    assert table["live_only"] == []


def test_neighbouring_minute_matches_with_lower_quality():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 3, 6)]}
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert table["trips"][0]["stops"][0]["match"]["quality"] == "±1 min"


def test_different_direction_does_not_match():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 1)]}
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert len(table["live_only"]) == 1
    assert table["trips"][0]["status"] == "nicht gesehen"


def test_plan_trip_inside_the_window_without_live_entry_is_not_seen():
    live = {"anchor": LIVE_ANCHOR, "departures": []}
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert [t["status"] for t in table["trips"]] == ["nicht gesehen"]
    assert table["summary"]["not_seen"] == 1


def test_plan_trip_outside_the_window_is_left_out():
    plan = {"1": _plan_entry("U1", 6, [("A", 400), ("B", 404)])}
    live = {"anchor": LIVE_ANCHOR, "departures": []}
    assert build_trip_table(plan, PLAN_ANCHOR, live)["trips"] == []


def test_platform_disambiguates_two_plan_stops_in_the_same_minute():
    plan = {
        "1": _plan_entry("U1", 6, [("A", 302)]),
        "2": _plan_entry("U1", 6, [("A", 302)]),
    }
    plan["2"]["stops"][0]["platform"] = "Gleis 2"
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [_live("U1", "A", 2, 6, platform="Gleis 2")],
    }
    table = build_trip_table(plan, PLAN_ANCHOR, live)
    confirmed = [t for t in table["trips"] if t["status"] == "bestätigt"]
    assert [t["service_id"] for t in confirmed] == ["2"]


def test_vehicles_model_and_changed_destination_are_carried_over():
    vehicles = [{"id": "x", "number": "417"}, {"id": "y", "number": "355"}]
    dep = _live("U1", "A", 2, 6, vehicles=vehicles)
    dep["line"]["direction"] = "Kurzziel"
    table = build_trip_table(
        PLAN, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": [dep]}
    )
    trip = table["trips"][0]
    assert trip["vehicles"] == "417 [DT5.1], 355 [DT5.1]"
    assert trip["model"] == "DT5"
    assert trip["destination_changed"] is True


def test_non_ubahn_lines_are_ignored():
    plan = {"1": _plan_entry("S1", 6, [("A", 302)])}
    plan["1"]["line"]["type"] = {"shortInfo": "S"}
    dep = _live("S1", "A", 2, 6)
    dep["line"]["type"] = {"shortInfo": "S"}
    table = build_trip_table(
        plan, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": [dep]}
    )
    assert table["trips"] == [] and table["summary"]["live_total"] == 0


def test_next_day_occurrence_of_a_service_id_is_a_separate_trip():
    plan = {"1": _plan_entry("U1", 6, [("A", 302), ("A", 302 + 1440)])}
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}
    table = build_trip_table(plan, PLAN_ANCHOR, live)
    assert [t["id"] for t in table["trips"]] == ["1"]


def test_missing_anchor_returns_empty_table():
    table = build_trip_table(PLAN, None, {"anchor": LIVE_ANCHOR, "departures": []})
    assert table == {"summary": {}, "trips": [], "live_only": []}


def test_cancelled_live_departure_marks_trip_as_cancelled():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [_live("U1", "A", 2, 6, cancelled=True)],
    }
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert table["trips"][0]["status"] == "ausgefallen"
    assert table["summary"]["cancelled"] == 1


def test_extra_live_departure_without_plan_is_reported_as_extra_trip():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [_live("U1", "A", 2, 1, service_id=5, extra=True)],
    }
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert table["live_only"][0]["reason"] == "Zusatzfahrt (extra)"
    assert table["summary"]["extra"] == 1


def _routed_plan():
    plan = {"1": _plan_entry("U1", 6, [("A", 302), ("C", 308)])}
    plan["1"]["subline"] = [
        {"id": sid, "name": sid.lower()} for sid in ("Z", "A", "B", "C", "D")
    ]
    return plan


def test_stations_between_anchors_get_interpolated_times():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}
    stops = build_trip_table(_routed_plan(), PLAN_ANCHOR, live)["trips"][0]["stops"]
    assert [s["station"] for s in stops] == ["z", "A", "b", "C", "d"]
    assert [s["planned"] for s in stops] == [None, "09:02", "09:05", "09:08", None]
    assert [s["interpolated"] for s in stops] == [False, False, True, False, False]
    assert stops[1]["match"] is not None


def test_anchors_that_do_not_fit_the_subline_are_shown_alone():
    plan = _routed_plan()
    plan["1"]["subline"] = [{"id": "X", "name": "x"}]
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}
    stops = build_trip_table(plan, PLAN_ANCHOR, live)["trips"][0]["stops"]
    assert [s["station"] for s in stops] == ["A", "C"]


def test_trip_row_reports_start_and_window_stop_separately():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "B", 6, 6)]}
    row = build_trip_table(PLAN, PLAN_ANCHOR, live)["trips"][0]
    assert (row["start_time"], row["start_station"]) == ("09:02", "A")
    assert (row["window_time"], row["window_station"]) == ("09:06", "B")


def test_cached_course_supplies_times_for_stations_outside_the_anchor_range():
    from hvv_map.trips import course_key, _plan_trips

    plan = _routed_plan()
    course = {
        "stops": [
            {
                "id": sid,
                "name": sid.lower(),
                "dep": f"2026-10-17T09:{minute:02d}",
                "arr": None,
                "dep_delay": 0 if sid == "D" else None,
            }
            for sid, minute in (("Z", 0), ("A", 2), ("B", 5), ("C", 8), ("D", 10))
        ]
    }
    key = course_key(_plan_trips(plan, PLAN_ANCHOR)[0])
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}

    stops = build_trip_table(plan, PLAN_ANCHOR, live, {key: course})["trips"][0][
        "stops"
    ]

    assert [s["planned"] for s in stops] == [
        "09:00",
        "09:02",
        "09:05",
        "09:08",
        "09:10",
    ]
    assert [s["source"] for s in stops] == [
        "course",
        "plan",
        "course",
        "plan",
        "course",
    ]
    assert not any(s["interpolated"] for s in stops)
    assert stops[4]["course_delay"] == 0


def test_trip_row_reports_the_time_span_of_its_route():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}
    row = build_trip_table(PLAN, PLAN_ANCHOR, live)["trips"][0]
    assert (row["route_start"], row["route_end"]) == (
        "2026-10-17T09:02",
        "2026-10-17T09:06",
    )


def test_failed_course_entry_falls_back_to_interpolation():
    from hvv_map.trips import _plan_trips, course_key

    plan = _routed_plan()
    key = course_key(_plan_trips(plan, PLAN_ANCHOR)[0])
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}

    table = build_trip_table(plan, PLAN_ANCHOR, live, {key: {"stops": None}})

    assert table["trips"][0]["stops"][2]["source"] == "interpolated"
