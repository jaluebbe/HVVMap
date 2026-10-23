"""Last known map position per U-Bahn unit, for the vehicle history on
static/TS.html: units no longer on the map are shown where they were last
seen. Kept in SQLite (survives Redis restarts), next to the GTI shapes
database; entries never expire, but only those of the last SHOW_MAX_AGE are
shown."""

import json
import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get(
    "UNIT_POSITIONS_DB_PATH",
    os.path.join(
        os.path.dirname(os.environ.get("GTI_SHAPES_DB_PATH", "")), "unit_positions.db"
    ),
)
RECORD_INTERVAL = 30  # seconds between writes from the positions loop
SHOW_MAX_AGE = 3 * 24 * 3600  # older last positions stay in the database only


def open_db(db_path: str | None = None) -> sqlite3.Connection:
    """Writer connection (positions loop); creates the table."""
    conn = sqlite3.connect(db_path or DEFAULT_DB_PATH, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS unit_positions ("
        "unit TEXT PRIMARY KEY, seen_at INTEGER, data_json TEXT)"
    )
    conn.commit()
    return conn


def open_reader(db_path: str | None = None) -> sqlite3.Connection | None:
    """Read-only connection (API); None before the first write."""
    db_path = db_path or DEFAULT_DB_PATH
    if not Path(db_path).is_file():
        return None
    return sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, check_same_thread=False, timeout=5
    )


def _units(feature: dict) -> list[str]:
    return [u for u in (feature["properties"].get("units") or "").split(" · ") if u]


def record_unit_positions(
    conn: sqlite3.Connection, features: list[dict], now_ts: float
) -> int:
    """Store the position of every unit on the map; returns their number."""
    rows = []
    for feature in features:
        units = _units(feature)
        properties = feature["properties"]
        data = json.dumps(
            {
                "coordinates": feature["geometry"]["coordinates"],
                "line": properties.get("line"),
                "icon": properties.get("icon"),
                "iconHeight": properties.get("iconHeight"),
                "label": properties.get("label"),
                "train": units,
            }
        )
        rows += [(unit, int(now_ts), data) for unit in units]
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO unit_positions (unit, seen_at, data_json) "
            "VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
    return len(rows)


def unit_history(
    conn: sqlite3.Connection | None, current: list[dict], now_ts: float
) -> dict:
    """FeatureCollection of the units not on the map now (`current` are the
    position features) seen within SHOW_MAX_AGE, at their last known position."""
    empty = {"type": "FeatureCollection", "features": []}
    if conn is None:
        return empty
    try:
        rows = conn.execute(
            "SELECT unit, seen_at, data_json FROM unit_positions "
            "WHERE seen_at >= ? ORDER BY unit",
            (int(now_ts - SHOW_MAX_AGE),),
        ).fetchall()
    except sqlite3.OperationalError:
        return empty
    on_map = {unit for feature in current for unit in _units(feature)}
    features = []
    for unit, seen_at, data_json in rows:
        if unit in on_map:
            continue
        entry = json.loads(data_json)
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
                    "seen_at": seen_at,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
