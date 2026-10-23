"""Last known map position per U-Bahn unit (hash hvv:vehicles:positions),
for the vehicle history on static/TS.html: units no longer on the map are
shown where they were last seen."""

import json

REDIS_UNIT_POSITIONS_KEY = "hvv:vehicles:positions"
RECORD_INTERVAL = 30  # seconds between writes from the positions loop
HISTORY_MAX_AGE = 14 * 24 * 3600  # older last positions are dropped


def _units(feature: dict) -> list[str]:
    return [u for u in (feature["properties"].get("units") or "").split(" · ") if u]


def record_unit_positions(redis_client, features: list[dict], now_ts: float) -> int:
    """Store the position of every unit on the map and drop entries older
    than HISTORY_MAX_AGE; returns the number of units written."""
    mapping = {}
    for feature in features:
        units = _units(feature)
        properties = feature["properties"]
        for unit in units:
            mapping[unit] = json.dumps(
                {
                    "coordinates": feature["geometry"]["coordinates"],
                    "line": properties.get("line"),
                    "icon": properties.get("icon"),
                    "iconHeight": properties.get("iconHeight"),
                    "label": properties.get("label"),
                    "train": units,
                    "seen_at": int(now_ts),
                }
            )
    if mapping:
        redis_client.hset(REDIS_UNIT_POSITIONS_KEY, mapping=mapping)
    stale = [
        unit
        for unit, raw in redis_client.hgetall(REDIS_UNIT_POSITIONS_KEY).items()
        if now_ts - json.loads(raw)["seen_at"] > HISTORY_MAX_AGE
    ]
    if stale:
        redis_client.hdel(REDIS_UNIT_POSITIONS_KEY, *stale)
    return len(mapping)


def unit_history(stored: dict, current: list[dict], now_ts: float) -> dict:
    """FeatureCollection of the units not on the map now (`current` are the
    position features), at their last known position."""
    on_map = {unit for feature in current for unit in _units(feature)}
    features = []
    for unit, raw in stored.items():
        if unit in on_map:
            continue
        entry = json.loads(raw)
        if now_ts - entry["seen_at"] > HISTORY_MAX_AGE:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": entry["coordinates"]},
                "properties": {
                    "unit": unit,
                    "line": entry["line"],
                    "icon": entry["icon"],
                    "iconHeight": entry["iconHeight"],
                    "label": entry["label"],
                    "train": entry["train"],
                    "seen_at": entry["seen_at"],
                },
            }
        )
    features.sort(key=lambda f: f["properties"]["unit"])
    return {"type": "FeatureCollection", "features": features}
