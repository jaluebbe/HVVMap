import json

from hvv_map.redis_client import get_redis_client
from hvv_map.unit_positions import (
    HISTORY_MAX_AGE,
    REDIS_UNIT_POSITIONS_KEY,
    record_unit_positions,
    unit_history,
)


def _feature(units, lon=10.0, line="U1"):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, 53.5]},
        "properties": {"units": units, "line": line, "icon": "i", "label": "Farmsen"},
    }


def test_positions_are_stored_per_unit_and_old_ones_dropped():
    redis_client = get_redis_client()
    redis_client.delete(REDIS_UNIT_POSITIONS_KEY)
    redis_client.hset(
        REDIS_UNIT_POSITIONS_KEY, "999", json.dumps({"seen_at": 0, "coordinates": []})
    )

    count = record_unit_positions(
        redis_client, [_feature("388 · 340"), _feature("")], HISTORY_MAX_AGE + 10
    )

    stored = redis_client.hgetall(REDIS_UNIT_POSITIONS_KEY)
    assert count == 2 and sorted(stored) == ["340", "388"]
    assert json.loads(stored["388"])["train"] == ["388", "340"]
    redis_client.delete(REDIS_UNIT_POSITIONS_KEY)


def test_history_lists_units_not_on_the_map():
    stored = {
        unit: json.dumps(
            {
                "coordinates": [10.1, 53.6],
                "line": "U1",
                "icon": "i",
                "iconHeight": 16,
                "label": "Ohlstedt",
                "train": [unit],
                "seen_at": seen_at,
            }
        )
        for unit, seen_at in (("388", 1000), ("417", 1000), ("101", -HISTORY_MAX_AGE))
    }

    history = unit_history(stored, [_feature("417")], 2000)

    [feature] = history["features"]
    assert feature["properties"]["unit"] == "388"
    assert feature["properties"]["seen_at"] == 1000
    assert feature["geometry"]["coordinates"] == [10.1, 53.6]
