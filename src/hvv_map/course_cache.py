"""Coverage of the trip course cache (hvv:trips:courses) for the live
serviceIds, for static/dev_courses.html."""

import statistics
from collections import defaultdict
from datetime import datetime

from hvv_map.gtilive_geojson import mode_for_line
from hvv_map.trips import alias_course_key

REFRESH_SECONDS = 300  # trip_courses.COURSE_REFRESH_SECONDS (not importable here)
MODE_ORDER = ["U", "S", "AKN", "R", "FERRY"]


def _ages(values: list[float]) -> dict:
    if not values:
        return {"median": None, "p90": None, "max": None}
    ordered = sorted(values)
    return {
        "median": int(statistics.median(ordered)),
        "p90": int(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]),
        "max": int(ordered[-1]),
    }


def _running(stops: list[dict], now: datetime) -> bool:
    times = [stop.get("dep") or stop.get("arr") for stop in stops]
    times = [datetime.fromisoformat(t) for t in times if t]
    return bool(times) and times[0] <= now <= times[-1]


def cache_summary(cached: dict, live: dict | None, now: datetime, now_ts: float):
    """Totals and per line: live serviceIds with a course, failed, without
    one, course ages (seconds); plus cached courses of trips running between
    anchors (no live departure)."""
    lines: dict[str, dict] = {}
    ids: dict[str, str] = {}
    for dep in (live or {}).get("departures", []):
        line = dep.get("line") or {}
        mode = mode_for_line(line)
        if not mode or dep.get("timeOffset") is None or dep.get("cancelled"):
            continue
        ids.setdefault(str(dep["serviceId"]), line.get("name", ""))
        lines.setdefault(line.get("name", ""), {"line": line.get("name"), "mode": mode})

    counts = defaultdict(lambda: {"live": 0, "ok": 0, "failed": 0, "missing": 0})
    ages = defaultdict(list)
    for service_id, name in ids.items():
        entry = cached.get(alias_course_key(service_id))
        counts[name]["live"] += 1
        if entry is None:
            counts[name]["missing"] += 1
        elif entry.get("stops") is None:
            counts[name]["failed"] += 1
        else:
            counts[name]["ok"] += 1
            ages[name].append(now_ts - entry["fetched_at"])

    prefix = alias_course_key("")
    between = [
        now_ts - entry["fetched_at"]
        for key, entry in cached.items()
        if key.startswith(prefix)
        and key[len(prefix) :] not in ids
        and entry.get("stops")
        and _running(entry["stops"], now)
    ]

    rows = []
    for name, info in lines.items():
        rows.append(
            {
                **info,
                **counts[name],
                "stale": sum(age >= REFRESH_SECONDS for age in ages[name]),
                "age": _ages(ages[name]),
            }
        )
    rows.sort(
        key=lambda row: (
            MODE_ORDER.index(row["mode"]) if row["mode"] in MODE_ORDER else 99,
            row["line"],
        )
    )
    all_ages = [age for values in ages.values() for age in values]
    total = {
        key: sum(row[key] for row in rows)
        for key in ("live", "ok", "failed", "missing", "stale")
    }
    return {
        "refresh_seconds": REFRESH_SECONDS,
        "total": {**total, "age": _ages(all_ages)},
        "between": {"count": len(between), "age": _ages(between)},
        "cached": len(cached),
        "lines": rows,
    }
