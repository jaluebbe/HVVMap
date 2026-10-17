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


def test_same_service_id_matches_despite_a_minute_offset():
    live = {
        "anchor": LIVE_ANCHOR,
        "departures": [_live("U1", "A", 3, 6, service_id=1)],
    }
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert table["trips"][0]["stops"][0]["match"]["quality"] == "ID, ±1 min"


def test_without_a_known_service_id_only_the_exact_minute_matches():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 3, 6)]}
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert table["summary"]["live_matched"] == 0
    assert table["live_only"][0]["reason"] == "ohne Plan-Treffer"


def test_unique_plan_stop_matches_despite_different_direction_id():
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 1)]}
    table = build_trip_table(PLAN, PLAN_ANCHOR, live)
    assert table["live_only"] == []
    quality = table["trips"][0]["stops"][0]["match"]["quality"]
    assert quality == "exakt, ohne Richtung"


def test_ambiguous_plan_stops_without_direction_stay_unmatched():
    plan = {
        "1": _plan_entry("U1", 6, [("A", 302)]),
        "2": _plan_entry("U1", 7, [("A", 302)]),
    }
    live = {"anchor": LIVE_ANCHOR, "departures": [_live("U1", "A", 2, 1)]}
    table = build_trip_table(plan, PLAN_ANCHOR, live)
    assert [e["reason"] for e in table["live_only"]] == ["mehrdeutig"]


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


def test_s_bahn_trips_are_matched_too():
    plan = {"1": _plan_entry("S1", 6, [("A", 302)])}
    plan["1"]["line"]["type"] = {"shortInfo": "S"}
    dep = _live("S1", "A", 2, 6)
    dep["line"]["type"] = {"shortInfo": "S"}
    table = build_trip_table(
        plan, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": [dep]}
    )
    assert table["trips"][0]["status"] == "bestätigt"


def test_repeated_live_entry_of_a_split_train_counts_as_duplicate_not_unmatched():
    first = _live("U1", "A", 2, 6, service_id=7, platform="Gleis 1")
    twin = _live("U1", "A", 2, 6, service_id=999, platform="Gleis 1")
    plan = {"1": _plan_entry("U1", 6, [("A", 302)])}
    table = build_trip_table(
        plan, PLAN_ANCHOR, {"anchor": LIVE_ANCHOR, "departures": [first, twin]}
    )
    assert table["live_only"] == []
    assert table["summary"]["quality"]["Duplikat"] == 1
    assert table["trips"][0]["live_ids"] == ["7", "999"]


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
    from hvv_map.trips import _plan_trips, alias_course_key, course_key

    first = _live("U1", "A", 2, 6, service_id=7, platform="Gleis 1")
    twin = _live("U1", "A", 2, 6, service_id=999, platform="Gleis 1")
    plan = {"1": _plan_entry("U1", 6, [("A", 302)])}
    key = course_key(_plan_trips(plan, PLAN_ANCHOR)[0])
    courses = {
        key: _course(
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
