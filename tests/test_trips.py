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


def test_a_minute_off_does_not_match_outside_the_s1():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 3, 6)]}
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    plan_trip = next(t for t in table["trips"] if not t["live_only"])
    assert plan_trip["status"] == "vermutlich ausgefallen"
    assert table["summary"]["quality"] == {}
    assert [t["service_id"] for t in table["trips"] if t["live_only"]] == ["900"]


def _s1_plan(destination="Hamburg Airport (Flughafen)"):
    entry = _plan_entry("S1", 1, [("A", 302)], destination)
    entry["line"]["type"] = {"shortInfo": "S"}
    return {"1": entry}


def _s1_live(offset, service_id, destination, platform="Gleis 1"):
    dep = _live("S1", "A", offset, 1, service_id=service_id, platform=platform)
    dep["line"] = {"name": "S1", "type": {"shortInfo": "S"}, "direction": destination}
    return dep


def test_s1_matches_a_minute_off_with_same_platform_and_destination():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [_s1_live(3, 5, "Hamburg Airport (Flughafen)")],
    }
    [trip] = build_trip_table(_s1_plan(), PLAN_ANCHOR, live)["trips"]
    assert trip["stops"][0]["match"]["quality"] == "±1 min"


def test_s1_a_minute_off_needs_the_same_platform():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [_s1_live(3, 5, "Hamburg Airport (Flughafen)", "Gleis 2")],
    }
    table = build_trip_table(_s1_plan(), PLAN_ANCHOR, live)
    assert table["summary"]["quality"] == {}


def test_s1_wing_train_takes_the_second_live_part_as_alias():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [
            _s1_live(2, 7397, "Hamburg Airport (Flughafen)"),
            _s1_live(2, 2046, "Poppenbüttel / Hamburg Airport (Flughafen)"),
        ],
    }
    table = build_trip_table(_s1_plan(), PLAN_ANCHOR, live)
    [trip] = table["trips"]
    assert trip["live_ids"] == ["2046", "7397"]
    assert trip["primary_ids"] == ["7397"]
    assert [a["service_id"] for a in trip["aliases"]] == ["2046"]
    assert table["summary"]["quality"] == {"exakt": 1, "Flügel": 1}
    assert table["live_only"] == []


def test_s1_wing_part_needs_the_plan_destination_in_its_own():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [
            _s1_live(2, 7397, "Hamburg Airport (Flughafen)"),
            _s1_live(2, 2046, "Wedel"),
        ],
    }
    table = build_trip_table(_s1_plan(), PLAN_ANCHOR, live)
    assert [t["service_id"] for t in table["trips"] if t["live_only"]] == ["2046"]


def test_conflicting_planned_platform_prevents_a_match():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [_live("U1", "A", 2, 6, platform="Gleis 2")],
    }
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert table["summary"]["quality"] == {}


def test_live_only_stops_across_04_00_stay_one_trip():
    live = {
        "anchor": {"date": "2026-10-17", "time": "03:50:00"},
        "departures": [
            _live("S1", "R", 2, 1, service_id=77),  # 03:52
            _live("S1", "W", 16, 1, service_id=77),  # 04:06
        ],
    }

    table = build_trip_table({}, PLAN_ANCHOR, live)

    [trip] = table["trips"]
    assert [s["station_id"] for s in trip["stops"]] == ["R", "W"]


def test_plan_stop_matches_despite_different_direction_id():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 1)]}
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert table["live_only"] == []
    assert table["trips"][0]["stops"][0]["match"]["quality"] == "exakt"


def test_same_direction_id_wins_among_live_departures_of_the_same_minute():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [
            _live("U1", "A", 2, 7, service_id=1),
            _live("U1", "A", 2, 6, service_id=2),
        ],
    }
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    plan_trip = next(t for t in table["trips"] if not t["live_only"])
    assert plan_trip["primary_ids"] == ["2"]


def test_plan_trip_inside_the_window_without_live_entry_is_not_seen():
    live = {"anchor": LIVE_ANCHOR, "departures": []}
    table = build_trip_table(
        PLAN, PLAN_ANCHOR, live, None, None, datetime(2026, 10, 17, 9, 30)
    )
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
    assert trip["vehicles"] == "DT5.1 (417), DT5.1 (355)"
    assert trip["model"] == "DT5"
    assert trip["destination_changed"] is True


def test_bus_lines_are_ignored():
    plan = {"1": _plan_entry("X1", 6, [("A", 302)])}
    plan["1"]["line"]["type"] = {"shortInfo": "Bus"}
    dep = _live("X1", "A", 2, 6)
    dep["line"]["type"] = {"shortInfo": "Bus"}
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
    extra = [t for t in table["trips"] if t["live_only"]]
    assert [t["status"] for t in extra] == ["bestätigt"]
    assert extra[0]["stops"][0]["match"]["extra"] is True
    assert table["live_only"] == []


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
    from hvv_map.trips import alias_course_key

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
    key = alias_course_key("900")
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


def test_cancelled_course_stops_and_live_departures_mark_their_rows():
    from hvv_map.trips import alias_course_key

    course = {
        "stops": [
            {
                "id": sid,
                "name": sid.lower(),
                "dep": f"2026-10-17T09:{minute:02d}",
                "arr": None,
                "dep_delay": None,
                "cancelled": sid == "D",
            }
            for sid, minute in (("Z", 0), ("A", 2), ("B", 5), ("C", 8), ("D", 10))
        ]
    }
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [_live("U1", "A", 2, 6, cancelled=True)],
    }
    courses = {alias_course_key("900"): course}

    row = build_trip_table(_routed_plan(), PLAN_ANCHOR, live, courses)["trips"][0]

    assert [s["cancelled"] for s in row["stops"]] == [False, True, False, False, True]


def test_trip_row_reports_the_time_span_of_its_route():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}
    row = build_trip_table(PLAN, PLAN_ANCHOR, live)["trips"][0]
    assert (row["route_start"], row["route_end"]) == (
        "2026-10-17T09:02",
        "2026-10-17T09:06",
    )


def test_failed_course_entry_falls_back_to_interpolation():
    from hvv_map.trips import alias_course_key

    plan = _routed_plan()
    key = alias_course_key("900")
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}

    table = build_trip_table(plan, PLAN_ANCHOR, live, {key: {"stops": None}})

    assert table["trips"][0]["stops"][2]["source"] == "interpolated"


def test_s_bahn_trips_are_matched_too():
    plan = {"1": _plan_entry("S1", 6, [("A", 302)])}
    plan["1"]["line"]["type"] = {"shortInfo": "S"}
    dep = _live("S1", "A", 2, 6)
    dep["line"]["type"] = {"shortInfo": "S"}
    table = build_trip_table(
        plan, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": [dep]}
    )
    assert table["trips"][0]["status"] == "bestätigt"


def _course(*stops):
    """stops: (id, name, arr, dep) with HH:MM strings."""
    return {
        "stops": [
            {
                "id": sid,
                "name": name,
                "arr": f"2026-10-17T{arr}" if arr else None,
                "dep": f"2026-10-17T{dep}" if dep else None,
                "dep_delay": None,
                "arr_delay": None,
                "platform": None,
            }
            for sid, name, arr, dep in stops
        ]
    }


def test_split_train_alias_course_reports_where_it_diverges():
    from hvv_map.trips import alias_course_key

    first = _s1_live(2, 7, "Hamburg Airport (Flughafen)")
    twin = _s1_live(2, 999, "Poppenbüttel / Hamburg Airport (Flughafen)")
    plan = _s1_plan()
    courses = {
        alias_course_key("7"): _course(
            ("A", "a", None, "09:02"),
            ("B", "b", "09:05", "09:06"),
            ("C", "c", "09:09", None),
        ),
        alias_course_key("999"): _course(
            ("A", "a", None, "09:02"),
            ("B", "b", "09:05", "09:07"),
            ("D", "d", "09:10", None),
        ),
    }

    row = build_trip_table(
        plan, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": [first, twin]}, courses
    )["trips"][0]

    assert [a["service_id"] for a in row["aliases"]] == ["999"]
    alias = row["alias_courses"][0]
    assert alias["shared_until"] == "b"
    assert [s["name"] for s in alias["stops"]] == ["a", "b", "d"]
    assert alias["destination"] == "d"
    assert row["destination"] == "c"  # announced destination not in the course


def test_coupled_train_parts_share_the_course_from_the_join_station_on():
    from hvv_map.trips import _shared_from, _shared_until

    main = _course(
        ("X", "x", None, "09:00"),
        ("J", "j", "09:05", "09:09"),
        ("E", "e", "09:12", None),
    )["stops"]
    other = _course(
        ("Y", "y", None, "09:01"),
        ("J", "j", "09:08", "09:09"),
        ("E", "e", "09:12", None),
    )["stops"]

    assert _shared_until(main, other) is None
    assert _shared_from(main, other) == "j"


def _typed(entry, short_info):
    entry["line"]["type"] = {"shortInfo": short_info}
    return entry


def test_all_modes_and_replacement_buses_are_matched():
    cases = [("A1", "A", "AKN"), ("RE8", "RE", "R"), ("64", "Schiff", "FERRY")]
    cases += [("RB61-SEV", "Bus", "SEV"), ("S1-SEV", "Bus", "SEV")]
    plan = {
        str(i): _typed(_plan_entry(name, 1, [("A", 302)]), info)
        for i, (name, info, _) in enumerate(cases, start=1)
    }
    departures = [
        {
            **_live(name, "A", 2, 1, service_id=i),
            "line": {**_typed(_live(name, "A", 2, 1), info)["line"]},
        }
        for i, (name, info, _) in enumerate(cases, start=1)
    ]

    table = build_trip_table(
        plan, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": departures}
    )

    modes = {t["line"]: t["mode"] for t in table["trips"]}
    assert modes == {name: mode for name, _, mode in cases}
    assert table["summary"]["live_matched"] == len(cases)


def test_lines_without_a_known_mode_are_ignored():
    plan = {"1": _typed(_plan_entry("X9", 1, [("A", 302)]), "Bus")}
    live = {"anchor": LIVE_ANCHOR, "departures": []}
    assert build_trip_table(plan, PLAN_ANCHOR, live)["trips"] == []


def test_running_trip_without_anchor_in_the_window_is_listed_as_between_anchors():
    plan = {"1": _plan_entry("U1", 6, [("A", 290), ("B", 320)])}  # 08:50 - 09:20
    live = {"anchor": LIVE_ANCHOR, "departures": []}

    table = build_trip_table(plan, PLAN_ANCHOR, live)

    assert [t["status"] for t in table["trips"]] == ["zwischen Ankern"]
    assert table["summary"]["between"] == 1
    assert table["trips"][0]["window_station"] == "B"  # next anchor ahead


def test_finished_or_future_trips_without_window_stop_are_not_listed():
    plan = {
        "1": _plan_entry("U1", 6, [("A", 200), ("B", 230)]),
        "2": _plan_entry("U1", 6, [("A", 400), ("B", 430)]),
    }
    table = build_trip_table(
        plan, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": []}
    )
    assert table["trips"] == []


def _with_delays(course, delays, fetched_at):
    """Copy of a _course with arr/dep delays per stop and a fetch time."""
    stops = [dict(s) for s in course["stops"]]
    for stop, delay in zip(stops, delays):
        stop["dep_delay"] = delay if stop["dep"] else None
        stop["arr_delay"] = delay if stop["arr"] else None
    return {"fetched_at": fetched_at, "stops": stops}


def _alias(stops, until=None, joined=None, fetched_at=0):
    return {
        "service_id": "999",
        "shared_until": until,
        "shared_from": joined,
        "fetched_at": fetched_at,
        "stops": stops,
    }


def _route(*delays):
    names = "abcd"
    return [
        {"station": names[i], "delay": d, "match": None} for i, d in enumerate(delays)
    ]


def test_shared_section_takes_the_delay_of_the_newer_course():
    from hvv_map.trips import unify_shared_delays

    course = _course(("A", "a", None, "09:00"), ("B", "b", "09:05", "09:06"))
    alias = _alias(
        _with_delays(course, [120, 180], 200)["stops"], until="b", fetched_at=200
    )
    rows = _route(0, 60)

    unify_shared_delays(rows, 100, [alias])

    assert [r["delay"] for r in rows] == [120, 180]
    assert [s["delay"] for s in alias["stops"]] == [120, 180]


def test_shared_section_keeps_the_newer_main_course():
    from hvv_map.trips import unify_shared_delays

    course = _course(("A", "a", None, "09:00"))
    alias = _alias(_with_delays(course, [240], 100)["stops"], until="a", fetched_at=100)
    rows = _route(60)

    unify_shared_delays(rows, 300, [alias])

    assert rows[0]["delay"] == alias["stops"][0]["delay"] == 60


def test_shared_section_prefers_live_delay_over_both_courses():
    from hvv_map.trips import unify_shared_delays

    course = _course(("A", "a", None, "09:00"))
    alias = _alias(_with_delays(course, [240], 900)["stops"], until="a", fetched_at=900)
    rows = _route(60)
    rows[0]["match"] = {"delay": 120}
    rows[0]["delay"] = 120

    unify_shared_delays(rows, 100, [alias])

    assert rows[0]["delay"] == alias["stops"][0]["delay"] == 120


def test_shared_section_with_equal_age_takes_the_larger_delay():
    from hvv_map.trips import unify_shared_delays

    course = _course(("A", "a", None, "09:00"))
    alias = _alias(_with_delays(course, [180], 100)["stops"], until="a", fetched_at=100)
    rows = _route(60)

    unify_shared_delays(rows, 100, [alias])

    assert rows[0]["delay"] == alias["stops"][0]["delay"] == 180


def test_unshared_alias_stops_keep_their_own_delay():
    from hvv_map.trips import unify_shared_delays

    course = _course(("A", "a", None, "09:00"), ("D", "d", "09:10", None))
    alias = _alias(
        _with_delays(course, [60, 300], 50)["stops"], until="a", fetched_at=50
    )
    rows = _route(0, None)
    rows[1]["station"] = "other"

    unify_shared_delays(rows, 10, [alias])

    assert [s["delay"] for s in alias["stops"]] == [60, 300]
    assert rows[1]["delay"] is None


def _vehicle_table(vehicles, memory=None, live_departures=True):
    deps = [_live("U1", "A", 2, 6, vehicles=vehicles)] if live_departures else []
    return build_trip_table(
        PLAN,
        PLAN_ANCHOR,
        {"anchor": LIVE_ANCHOR, "departures": deps},
        None,
        memory,
    )


def test_vehicles_are_remembered_while_the_trip_has_no_live_entry():
    from hvv_map.trips import remember_vehicles

    memory = {}
    first = _vehicle_table([{"id": "x", "number": "417"}])
    assert remember_vehicles(memory, first, 1000) is True

    later = _vehicle_table([], memory, live_departures=False)["trips"][0]

    assert later["vehicles"] == "DT5.1 (417)"
    assert later["model"] == "DT5"
    assert later["vehicles_remembered"] is True


def test_live_vehicles_win_over_the_memory_and_are_not_flagged():
    from hvv_map.trips import remember_vehicles

    memory = {}
    remember_vehicles(memory, _vehicle_table([{"id": "x", "number": "417"}]), 1000)

    row = _vehicle_table([{"id": "y", "number": "355"}], memory)["trips"][0]

    assert row["vehicles"] == "DT5.1 (355)"
    assert row["vehicles_remembered"] is False


def _course_table(vehicles, course_vehicles, memory=None):
    from hvv_map.trips import alias_course_key

    key = alias_course_key("900")
    stop = {"id": "A", "name": "a", "dep": "2026-10-17T09:02", "dep_delay": 0}
    courses = {key: {"fetched_at": 1, "stops": [stop], "vehicles": course_vehicles}}
    deps = [_live("U1", "A", 2, 6, vehicles=vehicles)]
    return build_trip_table(
        PLAN, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": deps}, courses, memory
    )["trips"][0]


def test_course_vehicles_fill_in_when_the_live_entry_has_none():
    row = _course_table([], [{"id": "x", "number": "417"}])

    assert row["vehicles"] == "DT5.1 (417)"
    assert row["model"] == "DT5"
    assert row["vehicles_from_course"] is True
    assert row["vehicles_remembered"] is False


def test_live_vehicles_win_over_the_course_and_the_course_over_the_memory():
    from hvv_map.trips import remember_vehicles

    memory = {}
    remember_vehicles(memory, _vehicle_table([{"id": "m", "number": "300"}]), 1000)

    live = _course_table([{"id": "y", "number": "355"}], [{"number": "417"}], memory)
    course = _course_table([], [{"number": "417"}], memory)

    assert (live["vehicles"], live["vehicles_from_course"]) == ("DT5.1 (355)", False)
    assert (course["vehicles"], course["vehicles_from_course"]) == ("DT5.1 (417)", True)


def test_a_course_without_stops_is_flagged_as_failed():
    from hvv_map.trips import alias_course_key

    key = alias_course_key("900")
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}

    def row(courses):
        return build_trip_table(PLAN, PLAN_ANCHOR, live, courses)["trips"][0]

    assert row({key: {"fetched_at": 1, "stops": None}})["course_failed"] is True
    assert row(None)["course_failed"] is False
    assert row({})["course_failed"] is False


def test_remembering_is_idempotent_and_forgets_old_trips():
    from hvv_map.trips import VEHICLE_MEMORY_SECONDS, remember_vehicles

    memory = {}
    table = _vehicle_table([{"id": "x", "number": "417"}])
    assert remember_vehicles(memory, table, 1000) is True
    assert remember_vehicles(memory, table, 1060) is False
    assert len(memory) == 1

    assert remember_vehicles(memory, {"trips": []}, 1000 + VEHICLE_MEMORY_SECONDS + 1)
    assert memory == {}


def test_trip_stays_listed_after_its_last_anchor_while_the_course_continues():
    from hvv_map.trips import LIVE_IDS_PREFIX, alias_course_key, course_key, _plan_trips

    plan = {"1": _plan_entry("U1", 6, [("A", 290), ("B", 295)])}
    course = {
        "stops": [
            {
                "id": sid,
                "name": sid,
                "dep": f"2026-10-17T{t}",
                "arr": None,
                "dep_delay": None,
            }
            for sid, t in (("A", "08:50"), ("B", "08:55"), ("C", "09:30"))
        ]
    }
    key = LIVE_IDS_PREFIX + course_key(_plan_trips(plan, PLAN_ANCHOR)[0])
    memory = {key: {"ids": ["900"], "aliases": [], "destination": "C", "seen_at": 1}}
    courses = {alias_course_key("900"): course}
    live = {"anchor": LIVE_ANCHOR, "departures": []}
    assert build_trip_table(plan, PLAN_ANCHOR, live, courses)["trips"] == []
    [trip] = build_trip_table(plan, PLAN_ANCHOR, live, courses, memory)["trips"]
    assert trip["status"] == "zwischen Ankern"
    assert trip["course_id"] == "900"


def test_live_ids_are_remembered_for_trips_between_anchors():
    from hvv_map.trips import remember_live_ids

    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    memory: dict = {}
    assert remember_live_ids(memory, table, 1.0)
    assert not remember_live_ids(memory, table, 2.0)
    [entry] = memory.values()
    assert entry["ids"] == ["900"] and entry["destination"] == "Ziel"


def test_live_departure_without_plan_becomes_a_trip_with_a_stable_course_key():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [
            _live("S7", "R", 2, 1, service_id=77),
            _live("S7", "W", 4, 1, service_id=77),
        ],
    }

    table = build_trip_table({}, PLAN_ANCHOR, live)

    [trip] = table["trips"]
    assert trip["live_only"] and trip["service_id"] == "77"
    assert trip["course_key"] == "livetrip:77@2026-10-17"
    assert [s["station_id"] for s in trip["stops"]] == ["R", "W"]
    assert table["live_only"] == []


def test_live_only_trip_is_kept_from_memory_until_its_end():
    from hvv_map.trips import remember_live_trips

    first = {
        "anchor": LIVE_ANCHOR,
        "departures": [_live("S7", "R", 2, 1, service_id=77)],
    }
    table = build_trip_table({}, PLAN_ANCHOR, first)
    memory: dict = {}
    assert remember_live_trips(memory, table, 1.0)
    assert not remember_live_trips(memory, table, 2.0)

    later = {"anchor": {"date": "2026-10-17", "time": "09:01:00"}, "departures": []}
    kept = build_trip_table({}, PLAN_ANCHOR, later, None, memory)
    assert [t["service_id"] for t in kept["trips"]] == ["77"]

    gone = {"anchor": {"date": "2026-10-17", "time": "09:10:00"}, "departures": []}
    assert build_trip_table({}, PLAN_ANCHOR, gone, None, memory)["trips"] == []


def test_unpaired_departure_with_a_plan_service_id_is_reported():
    plan = {"77": _plan_entry("U1", 6, [("A", 330)])}
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [_live("U1", "A", 2, 6, service_id=77)],
    }

    table = build_trip_table(plan, PLAN_ANCHOR, live)

    assert [t["service_id"] for t in table["trips"] if t["live_only"]] == ["77"]
    assert [e["reason"] for e in table["live_only"]] == ["Plan-ID ohne Paarung"]


def _missing_plan():
    plan = dict(PLAN)
    plan["2"] = _plan_entry("U1", 6, [("C", 303), ("D", 308)])
    return plan


def test_unseen_trip_is_missing_while_the_live_data_is_fresh():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}

    table = build_trip_table(_missing_plan(), PLAN_ANCHOR, live)

    status = {t["service_id"]: t["status"] for t in table["trips"]}
    assert status == {"1": "bestätigt", "2": "vermutlich ausgefallen"}
    assert table["summary"]["missing"] == 1


def test_unseen_trip_is_missing_even_if_nothing_at_all_is_live():
    live = {"anchor": LIVE_ANCHOR, "departures": []}

    table = build_trip_table(_missing_plan(), PLAN_ANCHOR, live)

    assert {t["status"] for t in table["trips"]} == {"vermutlich ausgefallen"}


def test_unseen_trip_stays_not_seen_when_the_live_data_is_stale():
    live = {"anchor": LIVE_ANCHOR, "departures": []}
    now = datetime(2026, 10, 17, 9, 10)

    table = build_trip_table(_missing_plan(), PLAN_ANCHOR, live, None, None, now)

    assert {t["status"] for t in table["trips"]} == {"nicht gesehen"}


def test_missing_trip_is_remembered_after_it_left_the_window():
    from hvv_map.trips import remember_missing

    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}
    table = build_trip_table(_missing_plan(), PLAN_ANCHOR, live)
    memory: dict = {}
    assert remember_missing(memory, table, 1000.0)
    assert not remember_missing(memory, table, 1001.0)

    later = {"anchor": {"date": "2026-10-17", "time": "09:05:00"}, "departures": []}
    table = build_trip_table(_missing_plan(), PLAN_ANCHOR, later, None, memory)

    status = {t["service_id"]: t["status"] for t in table["trips"]}
    assert status["2"] == "vermutlich ausgefallen"


def test_missing_trip_seen_live_after_all_is_forgotten():
    from hvv_map.trips import remember_missing

    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}
    memory: dict = {}
    remember_missing(memory, build_trip_table(_missing_plan(), PLAN_ANCHOR, live), 1.0)
    assert memory

    late = {
        "anchor": LIVE_ANCHOR,
        "departures": [_live("U1", "C", 3, 6, service_id=2)],
    }
    table = build_trip_table(_missing_plan(), PLAN_ANCHOR, late, None, memory)

    assert {t["service_id"]: t["status"] for t in table["trips"]}["2"] == "bestätigt"
    assert remember_missing(memory, table, 2.0)
    assert list(memory) == ["missing:1@2026-10-17T09:02"]


def test_api_module_imports_without_the_gti_client_dependency():
    import subprocess
    import sys

    code = "import sys; sys.modules['requests'] = None; import hvv_map.api"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr[-400:]


def test_regional_trip_is_expected_within_twenty_minutes_but_a_subway_within_five():
    r_type = {"shortInfo": "RE"}
    regional = _plan_entry("RE8", 1, [("A", 315)])  # 09:15
    regional["line"]["type"] = r_type
    plan = {"1": regional, "2": _plan_entry("U1", 6, [("A", 315)])}
    live = {"anchor": LIVE_ANCHOR, "departures": []}

    table = build_trip_table(plan, PLAN_ANCHOR, live)

    status = {t["line"]: t["status"] for t in table["trips"]}
    assert status.get("RE8") == "nicht gesehen"
    assert "U1" not in status


def test_alias_course_with_a_failed_main_course_fetch_does_not_crash():
    from hvv_map.trips import alias_course_key

    first = _s1_live(2, 7, "Hamburg Airport (Flughafen)")
    twin = _s1_live(2, 999, "Poppenbüttel / Hamburg Airport (Flughafen)")
    plan = _s1_plan()
    courses = {
        alias_course_key("7"): {
            "fetched_at": 1,
            "stops": None,
        },  # departureCourse failed
        alias_course_key("999"): _course(
            ("A", "a", None, "09:02"), ("B", "b", "09:05", None)
        ),
    }

    row = build_trip_table(
        plan, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": [first, twin]}, courses
    )["trips"][0]

    alias = row["alias_courses"][0]
    assert alias["shared_until"] is None
    assert alias["shared_from"] is None


def test_anchor_outside_the_course_is_left_out_and_reported():
    from hvv_map.trips import alias_course_key

    plan = {"1": _plan_entry("U1", 6, [("A", 302), ("B", 306)], "B")}
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 6)]}
    courses = {
        alias_course_key("900"): _course(
            ("A", "a", None, "09:02"), ("X", "x", "09:04", None)
        )
    }

    row = build_trip_table(plan, PLAN_ANCHOR, live, courses)["trips"][0]

    assert [s["station_id"] for s in row["stops"]] == ["A", "X"]
    assert row["anchors_outside_course"] == ["B"]
    assert row["destination"] == "Ziel"  # course destination only on the S1


def test_live_trip_of_a_wing_part_paired_later_is_dropped():
    from hvv_map.trips import remember_live_trips

    plan = _s1_plan()
    plan["1"]["stops"].append(
        dict(plan["1"]["stops"][0], timeOffset=310, station={"id": "B", "name": "B"})
    )
    first = {
        "anchor": LIVE_ANCHOR,
        "departures": [
            _s1_live(2, 7397, "Hamburg Airport (Flughafen)"),
            _s1_live(2, 2046, "Poppenbüttel / Hamburg Airport (Flughafen)", "Gleis 2"),
        ],
    }
    memory: dict = {}
    remember_live_trips(memory, build_trip_table(plan, PLAN_ANCHOR, first), 1.0)
    assert "trip:livetrip:2046@2026-10-17" in memory

    later_deps = [
        _s1_live(10, 7397, "Hamburg Airport (Flughafen)"),
        _s1_live(10, 2046, "Poppenbüttel / Hamburg Airport (Flughafen)"),
    ]
    for dep in later_deps:
        dep["station"] = {"id": "B", "name": "B"}
    later = {"anchor": LIVE_ANCHOR, "departures": later_deps}
    table = build_trip_table(plan, PLAN_ANCHOR, later, None, memory)

    assert not any(t["live_only"] for t in table["trips"])


def test_course_of_a_reused_service_id_in_the_other_direction_does_not_fit():
    from hvv_map.trips import _course_fits

    trip = {
        "stops": [
            {"station_id": "O", "planned": datetime(2026, 10, 22, 6, 49)},
            {"station_id": "I", "planned": datetime(2026, 10, 22, 7, 39)},
        ]
    }
    later_run = {
        "stops": [
            {"id": "I", "dep": "2026-10-22T07:33", "arr": None},
            {"id": "O", "dep": "2026-10-22T08:26", "arr": "2026-10-22T08:23"},
        ]
    }
    own_run = {
        "stops": [
            {"id": "O", "dep": "2026-10-22T06:49", "arr": None},
            {"id": "X", "dep": "2026-10-22T07:10", "arr": None},
        ]
    }
    assert not _course_fits(trip, later_run)
    assert _course_fits(trip, own_run)


def test_announced_destination_already_passed_gives_way_to_the_course_end():
    from hvv_map.trips import destination_from_courses

    course = [
        {"name": "Poppenbüttel", "dep": "2026-10-22T07:38"},
        {"name": "Ohlsdorf", "dep": "2026-10-22T07:49"},
        {"name": "Wedel", "dep": None},
    ]
    before = datetime(2026, 10, 22, 7, 45)
    after = datetime(2026, 10, 22, 7, 55)
    assert destination_from_courses(["Ohlsdorf"], [course], before) == "Ohlsdorf"
    assert destination_from_courses(["Ohlsdorf"], [course], after) == "Wedel"
    assert destination_from_courses(["Wedel"], [course], after) == "Wedel"


def test_live_delay_carries_on_behind_the_anchor_while_the_course_is_older():
    from hvv_map.trips import carry_live_delay

    def rows():
        return [
            {"match": {"delay": 240}, "delay": 240},
            {"match": None, "delay": 0},
            {"match": None, "delay": 60},
        ]

    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    old = {"fetched_at": datetime(2026, 10, 17, 8, 45, tzinfo=berlin).timestamp()}
    new = {"fetched_at": datetime(2026, 10, 17, 9, 5, tzinfo=berlin).timestamp()}
    anchor = datetime(2026, 10, 17, 9, 0)

    stale = rows()
    carry_live_delay(stale, old, anchor)
    fresh = rows()
    carry_live_delay(fresh, new, anchor)

    assert [r["delay"] for r in stale] == [240, None, None]
    assert [r["delay"] for r in fresh] == [240, 0, 60]


def test_in_repeated_hour_only_for_the_ambiguous_autumn_hour():
    from datetime import datetime

    from hvv_map.trips import in_repeated_hour

    assert in_repeated_hour(datetime(2026, 10, 25, 2, 5))
    assert not in_repeated_hour(datetime(2026, 10, 25, 1, 59))
    assert not in_repeated_hour(datetime(2026, 10, 25, 3, 0))
    assert not in_repeated_hour(datetime(2026, 3, 29, 2, 30))


def test_trip_end_includes_the_delay():
    from datetime import datetime

    from hvv_map.trips import trip_end

    planned = datetime(2026, 10, 25, 6, 31)
    trip = {"stops": [{"planned": planned, "match": {"delay": 420}}]}
    course = {"stops": [{"arr": "2026-10-25T06:31", "arr_delay": 480}]}

    assert trip_end(trip, None) == datetime(2026, 10, 25, 6, 38)
    assert trip_end(trip, course) == datetime(2026, 10, 25, 6, 39)
    assert trip_end({"stops": [{"planned": planned, "match": None}]}, None) == planned
