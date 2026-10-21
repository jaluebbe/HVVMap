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


def test_delay_is_reported_in_minutes():
    row = _row()
    row["stops"][0]["delay"] = 120

    [feature] = _positions(row, datetime(2026, 10, 17, 9, 3))

    assert feature["properties"]["delay"] == 2
    assert "+2 Minuten" in feature["properties"]["text"]


def test_cancelled_finished_and_unknown_station_trips_have_no_vehicle():
    assert _positions(_row(status="ausgefallen")) == []
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
