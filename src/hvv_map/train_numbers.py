"""Train numbers of S-Bahn, AKN and regional trains from the daily tables of
transitfeeds-de (numbers/train_numbers.py): train_numbers_YYYYMMDD.json in
TRAIN_NUMBERS_DIR, keyed by line, station DHID and planned departure."""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

NUMBERS_DIR = os.environ.get("TRAIN_NUMBERS_DIR", "")
MODES = {"S", "AKN", "R"}

_cache: dict[Path, tuple[float, dict]] = {}


def _table(day: date) -> dict:
    """Stop keys of one service day; {} if the file is missing."""
    if not NUMBERS_DIR:
        return {}
    path = Path(NUMBERS_DIR) / f"train_numbers_{day:%Y%m%d}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        stops = json.loads(path.read_text(encoding="utf-8")).get("stops", {})
    except (OSError, ValueError):
        return {}
    _cache[path] = (mtime, stops)
    return stops


def _station(global_id: str) -> str:
    return ":".join(global_id.split(":")[:3])


def lookup(line: str, stops: list[tuple[str | None, datetime]]) -> str:
    """Train number of a trip from its (station globalId, planned departure)
    stops: the numbers listed at every stop found ("11163/11263" for coupled
    trains), "" if none or ambiguous."""
    numbers, nexts, found_at = None, set(), 0
    keys = {
        (_station(global_id), planned.replace(second=0, microsecond=0))
        for global_id, planned in stops
        if global_id
    }
    for station, planned in keys:
        key = f"{line}|{station}|{planned:%Y-%m-%dT%H:%M}"
        # trips after midnight are in the previous service day's table
        for day in (planned.date(), planned.date() - timedelta(days=1)):
            entry = _table(day).get(key)
            if entry:
                found = {number for number, _ in entry}
                numbers = found if numbers is None else numbers & found
                nexts = {following for _, following in entry}
                found_at += 1
                break
    if not numbers:
        return ""
    if len(numbers) > 1 and found_at < 2 and len(nexts) > 1:
        return ""  # one stop: same-minute trains to different next stations
    return "/".join(sorted(numbers, key=int))
