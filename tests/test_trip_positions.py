from datetime import datetime

from hvv_map.stations import StationInfo
from hvv_map.trip_positions import build_positions

NOW = datetime(2026, 10, 17, 9, 2, 30)


def _station(sid, lon):
    return StationInfo(sid, sid, "Hamburg", lon, 53.5, sid)


STATIONS = {s: _station(s, i) for i, s in enumerate("ABCD")}


def _stop(sid, arr, dep, delay=0):
    at = f"2026-10-17T{dep or arr}"
    return {
        "station": sid,
        "station_id": sid,
        "arr": arr,
        "dep": dep,
        "at": at,
        "delay": delay,
    }


def _row(**extra):
    return {
        "id": "1",
        "line": "U1",
        "line_id": "HHA-U:U1_HHA-U",
        "mode": "U",
        "model": "DT5",
        "vehicles": "",
        "vehicles_remembered": False,
        "course_key": "1@2026-10-17T09:00",
        "destination": "D",
        "status": "bestätigt",
        "stops": [
            _stop("A", None, "09:00"),
            _stop("B", "09:04", "09:05"),
            _stop("C", "09:09", None),
        ],
        "alias_courses": [],
        **extra,
    }


def _positions(row, now=NOW):
    return build_positions({"trips": [row]}, now, STATIONS)["features"]


def test_vehicle_between_stops_is_placed_on_the_segment():
    [feature] = _positions(_row())

    # A departs 09:00, B is reached 09:04: 2:30 is 62.5 % of the way
    assert feature["geometry"]["coordinates"][0] == 0.625
    assert feature["properties"]["progress"] == 0.625
    assert feature["properties"]["mode"] == "U"
    assert feature["properties"]["text"] == "D<br>DT5"


def test_vehicle_at_a_stop_sits_on_the_station():
    [feature] = _positions(_row(), datetime(2026, 10, 17, 9, 4, 40))

    assert feature["geometry"]["coordinates"] == [1, 53.5]


def test_terminus_missing_from_stations_sits_at_the_end_of_its_track():
    from hvv_map.gti_shapes import open_db, store_segment

    conn = open_db(":memory:")
    store_segment(conn, "B", "X", [[1, 53.5], [1.5, 53.52], [1.9, 53.51]])
    row = _row()
    row["stops"][-1] = {**_stop("X", "09:09", None), "station": "Barmbek"}

    [feature] = build_positions(
        {"trips": [row]}, datetime(2026, 10, 17, 9, 9, 10), STATIONS, conn
    )["features"]

    assert feature["geometry"]["coordinates"] == [1.9, 53.51]
    assert feature["properties"]["terminus"] is True


def test_delay_is_reported_in_minutes():
    row = _row()
    row["stops"][0]["delay"] = 120

    [feature] = _positions(row, datetime(2026, 10, 17, 9, 3))

    assert feature["properties"]["delay"] == 2
    assert "+2 Minuten" in feature["properties"]["text"]


def test_cancelled_finished_and_unknown_station_trips_have_no_vehicle():
    cancelled = _row(status="ausgefallen")
    cancelled["stops"] = [{**s, "cancelled": True} for s in cancelled["stops"]]
    assert _positions(cancelled) == []
    assert _positions(_row(), datetime(2026, 10, 17, 10, 0)) == []
    row = _row()
    row["stops"][0]["station_id"] = "unknown"
    assert _positions(row) == []


def test_replacement_bus_uses_the_mode_of_the_rail_line():
    [feature] = _positions(_row(line="S7-SEV", mode="SEV"))

    assert feature["properties"]["mode"] == "S"


def _alias_stops(delay=0):
    def stop(sid, arr, dep):
        return {
            "id": sid,
            "name": sid,
            "arr": f"2026-10-17T{arr}" if arr else None,
            "dep": f"2026-10-17T{dep}" if dep else None,
            "delay": delay,
        }

    return [
        stop("A", None, "09:00"),
        stop("B", "09:04", "09:05"),
        stop("D", "09:09", None),
    ]


def _split_alias(until="B"):
    return {
        "service_id": "999",
        "destination": "Other",
        "shared_until": until,
        "shared_from": None,
        "stops": _alias_stops(),
    }


def test_split_train_has_no_second_vehicle_inside_the_shared_section():
    assert len(_positions(_row(alias_courses=[_split_alias()]))) == 1


def test_split_train_shows_the_other_branch_after_the_split():
    now = datetime(2026, 10, 17, 9, 7)
    row = _row(alias_courses=[_split_alias()])

    features = _positions(row, now)

    assert sorted(f["properties"]["journeyID"] for f in features) == ["1", "999"]
    other = next(f for f in features if f["properties"]["journeyID"] == "999")
    assert other["properties"]["label"] == "Other"
    assert other["geometry"]["coordinates"][0] == 2.0  # halfway between B(1) and D(3)


def _coupled(own_announced, wing_announced):
    alias = {**_split_alias(), "destination_announced": wing_announced}
    return _row(
        alias_courses=[alias],
        course_id="1",
        destination=own_announced,
        destination_announced=own_announced,
    )


def _label(row, now=NOW):
    features = _positions(row, now)
    return {f["properties"]["journeyID"]: f["properties"]["label"] for f in features}


def test_coupled_train_shows_the_destinations_both_courses_contain():
    assert _label(_coupled("C", "D / C")) == {"1": "D / C"}


def test_coupled_train_keeps_its_own_destination_after_the_split():
    labels = _label(_coupled("C", "D / C"), datetime(2026, 10, 17, 9, 7))
    assert labels == {"1": "C", "999": "Other"}


def test_announced_destination_missing_from_both_courses_is_dropped():
    assert _label(_coupled("C", "Wedel")) == {"1": "C"}


def test_without_any_announced_destination_in_the_courses_their_ends_are_shown():
    assert _label(_coupled("Wedel", "Wedel")) == {"1": "C / D"}


def test_without_an_own_course_the_label_stays():
    row = {**_coupled("C", "D / C"), "course_id": None}
    assert _label(row) == {"1": "C"}


def _wing_part(**extra):
    """A trip of its own for the wing part 999 (e.g. a plan trip from the
    split station on), running on the alias course's stops."""
    return _row(
        id="2",
        primary_ids=["999"],
        destination="Other",
        stops=[
            _stop("A", None, "09:00"),
            _stop("B", "09:04", "09:05"),
            _stop("D", "09:09", None),
        ],
        **extra,
    )


def _ids(rows, now=NOW):
    table = {"trips": rows}
    return sorted(
        f["properties"]["journeyID"]
        for f in build_positions(table, now, STATIONS)["features"]
    )


def test_trip_of_a_wing_part_is_not_drawn_while_the_train_runs_as_one():
    main = _row(primary_ids=["1"], alias_courses=[_split_alias()])
    assert _ids([main, _wing_part()]) == ["1"]


def test_trip_of_a_wing_part_draws_the_branch_after_the_split():
    main = _row(primary_ids=["1"], alias_courses=[_split_alias()])
    assert _ids([main, _wing_part()], datetime(2026, 10, 17, 9, 7)) == ["1", "2"]


def test_trip_without_live_departure_on_another_trips_course_is_not_drawn():
    main = _row(primary_ids=["1"], alias_courses=[_split_alias()])
    remembered = _row(id="3", primary_ids=[], course_id="999")
    assert _ids([main, remembered]) == ["1"]


def test_unconfirmed_trip_at_the_place_of_a_confirmed_one_is_not_drawn():
    confirmed = _row(primary_ids=["1"])
    joined = _row(id="5", primary_ids=[])
    elsewhere = _row(id="6", primary_ids=[], line="U2")
    assert _ids([confirmed, joined, elsewhere]) == ["1", "6"]


def test_two_plan_trips_of_one_vehicle_are_drawn_once():
    own = _row(id="70311", service_id="70311", primary_ids=["70311"])
    feeder = _row(id="70861", service_id="70861", primary_ids=["70311"], line="U2")
    assert _ids([feeder, own]) == ["70311"]


def test_trips_sharing_one_course_are_drawn_once():
    flensburg = _row(id="2468", service_id="2468", primary_ids=[], course_id="2468")
    kiel = _row(
        id="2576", service_id="2576", primary_ids=[], course_id="2468", line="U2"
    )
    assert _ids([kiel, flensburg]) == ["2468"]


def test_confirmed_trips_of_one_direction_at_the_same_spot_are_one_coupled_train():
    kiel = _row(id="2639", primary_ids=["2639"], direction_id=1, destination="Kiel Hbf")
    flensburg = _row(
        id="2454", primary_ids=["2454"], direction_id=1, destination="Flensburg"
    )
    back = _row(id="9", primary_ids=["9"], direction_id=6, destination="Hamburg Hbf")

    features = _positions_of([kiel, flensburg, back])

    labels = sorted(f["properties"]["label"] for f in features)
    assert labels == ["Hamburg Hbf", "Kiel Hbf / Flensburg"]
    coupled = next(f for f in features if "/" in f["properties"]["label"])
    assert coupled["properties"]["text"].startswith("Kiel Hbf / Flensburg<br>")


def test_trip_at_its_terminus_is_not_merged_with_one_starting_there():
    arriving = _row(id="1", primary_ids=[], vehicles="DT5.1 (375)")
    starting = _row(
        id="2",
        primary_ids=["2"],
        stops=[_stop("C", "09:09", "09:12"), _stop("D", "09:15", None)],
    )

    features = _positions_of([arriving, starting], datetime(2026, 10, 17, 9, 9, 10))

    assert sorted(f["properties"]["journeyID"] for f in features) == ["1", "2"]
    assert next(f for f in features if f["properties"]["journeyID"] == "1")[
        "properties"
    ]["terminus"]


def _positions_of(rows, now=NOW):
    return build_positions({"trips": rows}, now, STATIONS)["features"]


def test_of_unconfirmed_trips_at_one_spot_the_one_on_its_own_course_stays():
    joined = _row(id="4654", service_id="4654", primary_ids=[], course_id="7211")
    own = _row(id="3932", service_id="3932", primary_ids=[], course_id="3932")
    assert _ids([joined, own]) == ["3932"]


class _FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.expiry = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.expiry[key] = ex


def test_run_positions_stores_the_feature_collection(monkeypatch):
    import json

    from hvv_map import trip_positions as module

    blob = json.dumps({"data": {}})
    redis_client = _FakeRedis(
        {
            module.REDIS_BY_SERVICE_KEY: blob,
            module.REDIS_ANCHOR_KEY: blob,
            module.REDIS_LIVE_KEY: blob,
        }
    )
    monkeypatch.setattr(module, "parse_anchor", lambda data: None)
    monkeypatch.setattr(module, "build_trip_table", lambda *a: {"trips": [_row()]})
    monkeypatch.setattr(module, "load_stations", lambda client: list(STATIONS.values()))

    count = module.run_positions(redis_client, None, NOW)

    stored = json.loads(redis_client.values[module.REDIS_POSITIONS_KEY])
    assert count == 1
    assert len(stored["data"]["features"]) == 1
    assert redis_client.expiry[module.REDIS_POSITIONS_KEY] == module.POSITIONS_TTL


def test_run_positions_without_source_data_stores_nothing():
    from hvv_map import trip_positions as module

    redis_client = _FakeRedis()

    assert module.run_positions(redis_client, None, NOW) == 0
    assert module.REDIS_POSITIONS_KEY not in redis_client.values


def _redis_with_source_data(module):
    import json

    blob = json.dumps({"data": {}})
    return _FakeRedis(
        {
            module.REDIS_BY_SERVICE_KEY: blob,
            module.REDIS_ANCHOR_KEY: blob,
            module.REDIS_LIVE_KEY: blob,
        }
    )


def test_table_is_rebuilt_only_when_the_source_data_changed(monkeypatch):
    from hvv_map import trip_positions as module

    builds = []
    monkeypatch.setattr(module, "parse_anchor", lambda data: None)
    monkeypatch.setattr(module, "load_stations", lambda client: [])
    monkeypatch.setattr(
        module, "build_trip_table", lambda *a: builds.append(1) or {"trips": []}
    )
    redis_client = _redis_with_source_data(module)
    state = module.PositionsState()

    module.run_positions(redis_client, None, NOW, state)
    module.run_positions(redis_client, None, NOW, state)
    state.checked = float("-inf")  # reload interval elapsed, data unchanged
    module.run_positions(redis_client, None, NOW, state)
    assert len(builds) == 1

    redis_client.values[module.REDIS_LIVE_KEY] = '{"data": {"x": 1}}'
    state.checked = float("-inf")
    module.run_positions(redis_client, None, NOW, state)
    assert len(builds) == 2


def test_vehicle_on_a_segment_without_stored_track_is_flagged_straight():
    [feature] = _positions(_row())

    assert feature["properties"]["straight"] is True


def test_vehicle_at_a_stop_is_never_flagged_straight():
    [feature] = _positions(_row(), datetime(2026, 10, 17, 9, 4, 40))

    assert feature["properties"]["straight"] is False


def test_vehicle_on_a_stored_track_is_not_flagged_straight():
    from hvv_map.gti_shapes import open_db, store_segment

    conn = open_db(":memory:")
    store_segment(
        conn, "A", "B", [[0, 53.5], [0.03, 53.52], [0.06, 53.52], [0.1, 53.5]]
    )

    [feature] = build_positions({"trips": [_row()]}, NOW, STATIONS, conn)["features"]

    assert feature["properties"]["straight"] is False
    assert feature["geometry"]["coordinates"][1] > 53.5  # follows the bend


def test_ubahn_popup_shows_the_vehicles_instead_of_the_model():
    [feature] = _positions(_row(vehicles="DT5.1 (417)"))

    assert feature["properties"]["text"] == "D<br>DT5.1 (417)"


def test_other_modes_keep_the_model_in_the_popup():
    [feature] = _positions(_row(mode="S", vehicles="4711"))

    assert feature["properties"]["text"] == "D<br>DT5"


def test_run_positions_persists_the_vehicle_memory(monkeypatch):
    import json

    from hvv_map import trip_positions as module

    row = _row(vehicles="DT5.1 (417)")
    redis_client = _redis_with_source_data(module)
    monkeypatch.setattr(module, "parse_anchor", lambda data: None)
    monkeypatch.setattr(module, "load_stations", lambda client: [])
    monkeypatch.setattr(module, "build_trip_table", lambda *a: {"trips": [row]})

    module.run_positions(redis_client, None, NOW, module.PositionsState())

    stored = json.loads(redis_client.values[module.REDIS_VEHICLES_KEY])["data"]
    assert stored[row["course_key"]]["vehicles"] == "DT5.1 (417)"


def test_missing_trip_is_not_placed_on_the_map():
    row = {**_row(), "status": "vermutlich ausgefallen"}

    assert build_positions({"trips": [row]}, NOW, STATIONS)["features"] == []


def _cancelled(row, *names):
    for stop in row["stops"]:
        stop["cancelled"] = stop["station"] in names
    return row


def test_cancelled_first_stop_is_not_driven():
    # A cancelled: the train starts at B (09:05), so at 09:02:30 not yet
    assert _positions(_cancelled(_row(), "A")) == []
    [feature] = _positions(_cancelled(_row(), "A"), datetime(2026, 10, 17, 9, 6))
    assert 0 < feature["properties"]["progress"] < 1


def test_cancelled_last_stops_end_the_trip_early_with_their_label():
    row = _cancelled(_row(), "C")
    [feature] = _positions(row)
    assert feature["properties"]["label"] == "B"
    assert _positions(row, datetime(2026, 10, 17, 9, 7)) == []


def test_trip_with_every_stop_cancelled_has_no_vehicle():
    assert _positions(_cancelled(_row(), "A", "B", "C")) == []


def test_ubahn_vehicle_lists_its_unit_numbers():
    [feature] = _positions(_row(vehicles="DT5.1 (388), DT5.2 (440)"))
    assert feature["properties"]["units"] == "388 · 440"


def test_other_modes_have_no_unit_numbers():
    [feature] = _positions(_row(mode="S", vehicles="474 (4011)"))
    assert feature["properties"]["units"] == ""


def test_vehicle_at_its_last_stop_is_flagged_terminus():
    [between] = _positions(_row())
    [arrived] = _positions(_row(), datetime(2026, 10, 17, 9, 9, 10))

    assert "terminus" not in between["properties"]
    assert arrived["properties"]["terminus"] is True


def test_arrival_at_the_terminus_is_recorded_at_once(monkeypatch):
    from hvv_map import trip_positions as module

    recorded = []
    row = _row(vehicles="DT5.1 (417)")
    redis_client = _redis_with_source_data(module)
    monkeypatch.setattr(module, "parse_anchor", lambda data: None)
    monkeypatch.setattr(module, "load_stations", lambda client: list(STATIONS.values()))
    monkeypatch.setattr(module, "build_trip_table", lambda *a: {"trips": [row]})
    monkeypatch.setattr(module, "open_units_db", lambda: object())
    monkeypatch.setattr(
        module, "record_unit_positions", lambda conn, f, *a: recorded.append(len(f))
    )
    state = module.PositionsState()
    arrival = datetime(2026, 10, 17, 9, 9, 10)

    module.run_positions(redis_client, None, NOW, state)  # due: first write
    module.run_positions(redis_client, None, NOW, state)  # not due, not arrived
    module.run_positions(redis_client, None, arrival, state)  # arrived
    module.run_positions(redis_client, None, arrival, state)  # already recorded

    assert recorded == [1, 1]


def test_trip_ends_give_the_terminus_and_planned_arrival():
    from datetime import timezone

    from hvv_map.trip_positions import _trip_ends

    row = {**_row(), "route_end": "2026-10-17T09:09"}  # summer time: UTC+2
    unknown = _row(id="2")
    unknown["stops"] = [{**s, "station_id": "nowhere"} for s in unknown["stops"]]

    ends = _trip_ends({"trips": [row, unknown]}, STATIONS)

    at = int(datetime(2026, 10, 17, 7, 9, tzinfo=timezone.utc).timestamp())
    assert ends == {"1": {"coordinates": [2, 53.5], "at": at}}


def test_after_a_cancelled_stop_only_known_stops_count_as_served():
    from hvv_map.trip_positions import _served

    a, b = {"station": "A", "known": True}, {"station": "B", "cancelled": True}
    c, d = {"station": "C", "known": False}, {"station": "D", "known": False}

    assert _served([a, b, c, d]) == [a]
    assert _served([a, b, c, {**d, "known": True}]) == [a, b, c, {**d, "known": True}]


def test_partially_cancelled_trip_runs_to_its_last_served_stop():
    row = _row(status="ausgefallen")
    row["stops"][-1] = {**row["stops"][-1], "cancelled": True}

    [between] = _positions(row)
    [arrived] = _positions(row, datetime(2026, 10, 17, 9, 4, 40))

    assert between["properties"]["label"] == "B"
    assert arrived["properties"]["terminus"] is True
    assert _positions(row, datetime(2026, 10, 17, 9, 7)) == []


def test_trip_ends_at_its_last_served_stop():
    from hvv_map.trip_positions import _trip_ends

    row = _row()
    row["stops"][-1] = {**row["stops"][-1], "cancelled": True}

    ends = _trip_ends({"trips": [row]}, STATIONS)

    assert ends["1"]["coordinates"] == [1, 53.5]  # B, not the cancelled C
