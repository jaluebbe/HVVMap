from hvv_map.unit_positions import open_db, open_reader, record_unit_positions
from hvv_map.unit_positions import unit_history


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


def test_history_lists_units_not_on_the_map_without_expiry(tmp_path):
    path = str(tmp_path / "units.db")
    record_unit_positions(open_db(path), [_feature("388 · 340"), _feature("417")], 0)

    history = unit_history(open_reader(path), [_feature("417")])

    assert [f["properties"]["unit"] for f in history["features"]] == ["340", "388"]
    feature = history["features"][1]
    assert feature["properties"]["seen_at"] == 0
    assert feature["properties"]["train"] == ["388", "340"]
    assert feature["geometry"]["coordinates"] == [10.0, 53.5]


def test_history_is_empty_before_the_first_write(tmp_path):
    assert open_reader(str(tmp_path / "missing.db")) is None
    assert unit_history(None, [])["features"] == []
