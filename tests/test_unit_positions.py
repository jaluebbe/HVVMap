from hvv_map.unit_positions import open_db, open_reader, record_unit_positions
from hvv_map.unit_positions import SHOW_MAX_AGE, unit_history


def _feature(units, lon=10.0, line="U1"):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, 53.5]},
        "properties": {"units": units, "line": line, "icon": "i", "label": "Farmsen"},
    }


def test_positions_are_stored_per_unit_and_replaced(tmp_path):
    path = str(tmp_path / "units.db")
    conn = open_db(path)

    record_unit_positions(conn, [_feature("388 · 340", lon=9.0)], 1000)
    count = record_unit_positions(conn, [_feature("388 · 340"), _feature("")], 2000)

    rows = conn.execute("SELECT unit, seen_at FROM unit_positions ORDER BY unit")
    assert count == 2 and rows.fetchall() == [("340", 2000), ("388", 2000)]


def test_history_lists_recent_units_not_on_the_map(tmp_path):
    path = str(tmp_path / "units.db")
    conn = open_db(path)
    record_unit_positions(conn, [_feature("388 · 340"), _feature("417")], 0)
    record_unit_positions(conn, [_feature("101")], -SHOW_MAX_AGE)  # kept, not shown

    history = unit_history(open_reader(path), [_feature("417")], SHOW_MAX_AGE - 1)

    assert [f["properties"]["unit"] for f in history["features"]] == ["340", "388"]
    feature = history["features"][1]
    assert feature["properties"]["seen_at"] == 0
    assert feature["properties"]["train"] == ["388", "340"]
    assert feature["geometry"]["coordinates"] == [10.0, 53.5]


def test_history_is_empty_before_the_first_write(tmp_path):
    assert open_reader(str(tmp_path / "missing.db")) is None
    assert unit_history(None, [], 0)["features"] == []


def test_old_positions_stay_in_the_database(tmp_path):
    conn = open_db(str(tmp_path / "units.db"))
    record_unit_positions(conn, [_feature("101")], 0)

    assert unit_history(conn, [], 10 * SHOW_MAX_AGE)["features"] == []
    assert conn.execute("SELECT unit FROM unit_positions").fetchall() == [("101",)]


def test_ghost_train_omits_partners_running_again(tmp_path):
    conn = open_db(str(tmp_path / "units.db"))
    record_unit_positions(conn, [_feature("396 · 456")], 0)

    history = unit_history(conn, [_feature("314 · 456")], 1)

    [feature] = history["features"]
    assert feature["properties"]["unit"] == "396"
    assert feature["properties"]["train"] == ["396"]


def test_fleet_counts_every_unit_ever_seen_per_series(tmp_path):
    from hvv_map.unit_positions import fleet_summary

    conn = open_db(str(tmp_path / "units.db"))
    record_unit_positions(conn, [_feature("101 · 150"), _feature("999")], 0)

    fleet = fleet_summary(conn, [_feature("301 · 419")], 10 * SHOW_MAX_AGE)

    dt41 = fleet["series"][0]
    assert dt41["series"] == "DT4.1" and dt41["seen"] == 1 and dt41["total"] == 30
    assert 101 not in dt41["missing"] and 102 in dt41["missing"]
    dt426 = fleet["series"][2]
    assert dt426["series"] == "DT4.26" and dt426["ranges"] == [[141, 141], [143, 151]]
    assert dt426["total"] == 9 and 150 not in dt426["missing"]  # retired
    assert fleet["totals"]["DT4"] == {"total": 125, "seen": 1, "on_map": 0}
    assert fleet["totals"]["DT5"] == {"total": 163, "seen": 2, "on_map": 2}
    assert fleet["units"]["101"]["last_seen"] == 0
    assert fleet["unknown"] == ["999"]


def test_fleet_without_database_counts_the_map_only():
    from hvv_map.unit_positions import fleet_summary

    fleet = fleet_summary(None, [_feature("417")], 0)

    assert fleet["totals"]["DT5"]["seen"] == 1 and fleet["units"] == {}


def test_ghost_lost_mid_route_moves_to_its_terminus_once_due(tmp_path):
    conn = open_db(str(tmp_path / "units.db"))
    feature = _feature("301")
    feature["properties"]["journeyID"] = "21745"
    end = {"coordinates": [9.97, 53.55], "at": 600}
    record_unit_positions(conn, [feature], 0, {"21745": end})

    before = unit_history(conn, [], 599)["features"][0]
    after = unit_history(conn, [], 601)["features"][0]

    assert before["geometry"]["coordinates"] == [10.0, 53.5]
    assert after["geometry"]["coordinates"] == [9.97, 53.55]
    assert after["properties"]["seen_at"] == 600
