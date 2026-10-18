"""Real route geometry and scheduled per-stop timing for resolved gti-live
sublines, fetched via departureCourse - one call per distinct subline
(line_id + matched station-id sequence, see departures.match_sublines), not
per trip: a subline's shape and schedule are stable, only per-trip delay
varies. Once a subline is resolved here, no further departureCourse calls
are needed for it.

departureCourse's own station ids match listLines/hvv:stations directly
(e.g. "Master:81950"), so segment geometry is keyed on those - no
stopPointKey crosswalk needed.

Two tables in one SQLite file (data/gti_shapes.db by default):
- segments: per (from_station_id, to_station_id) LineString geometry, from
  one courseElement's path.track.
- subline_schedule: per subline_key, the per-stop time offset (seconds from
  the first stop's departure) - for later live ETA estimation.

Not part of the continuous fetcher loop - run manually or on its own
schedule, same as departures.py, and after it (needs hvv:departures:by_service
and hvv:departures:anchor_start).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from hvv_map.geometry import haversine

if TYPE_CHECKING:
    # Type-only: keeps gti_client's requests dependency out of this module's
    # import graph, since gtilive_geojson.py (imported by api.py) reads
    # segments/schedules from here.
    from hvv_map.gti_client import GtiClient

BERLIN_TZ = ZoneInfo("Europe/Berlin")

DEFAULT_DB_PATH = os.environ.get("GTI_SHAPES_DB_PATH", "gti_shapes.db")

# Read locally rather than imported from departures.py/redis_client.py: both
# pull in gti_client's requests dependency at module level, which the api
# image doesn't install (see api.py's own REDIS_BY_SERVICE_KEY constant).
REDIS_BY_SERVICE_KEY = "hvv:departures:by_service"
REDIS_ANCHOR_KEY = "hvv:departures:anchor_start"

Coordinates = list[list[float]]  # [[lon, lat], ...] - GeoJSON-ready


def subline_key(line_id: str, station_ids: tuple[str, ...]) -> str:
    return line_id + "|" + "|".join(station_ids)


def open_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    if (
        db_path != ":memory:"
        and not os.path.isabs(db_path)
        and not os.path.exists(db_path)
    ):
        print(
            f"[hvv_map.gti_shapes] warning: {db_path!r} does not exist yet in "
            f"{os.getcwd()!r} - creating a NEW, EMPTY database there.",
            file=sys.stderr,
        )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS segments ("
        "from_id TEXT, to_id TEXT, coordinates_json TEXT, last_seen INTEGER, "
        "PRIMARY KEY (from_id, to_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS subline_schedule ("
        "subline_key TEXT PRIMARY KEY, service_id TEXT, "
        "stop_offsets_json TEXT, resolved_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS course_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, called_at INTEGER, "
        "subline_key TEXT, line_name TEXT, service_id TEXT, station TEXT, "
        "dep_time TEXT, result TEXT, segments INTEGER, delays_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS variant_sublines ("
        "variant_key TEXT PRIMARY KEY, subline_json TEXT, resolved_at INTEGER)"
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(course_log)")}
    if "meta_json" not in columns:
        conn.execute("ALTER TABLE course_log ADD COLUMN meta_json TEXT")
    return conn


def get_segment(
    conn: sqlite3.Connection, from_id: str, to_id: str
) -> Coordinates | None:
    row = conn.execute(
        "SELECT coordinates_json FROM segments WHERE from_id = ? AND to_id = ?",
        (from_id, to_id),
    ).fetchone()
    return json.loads(row[0]) if row else None


EXTRA_COURSE_TRIES = 3  # further trips per subline while hops lack a segment


def subline_hops(
    conn: sqlite3.Connection | None, station_ids: tuple[str, ...]
) -> list[tuple[str, str, Coordinates | None]]:
    """(from_id, to_id, coordinates) per hop of a subline; coordinates is None
    where no track is stored. A hop without its own segment reuses the
    opposite direction's track (reversed) if known, else one that
    spans it when trips skip the declared intermediate stop(s) (e.g. a
    variant with an extra, rarely served pier), so from_id/to_id may then
    be further apart than neighbours (e.g. a replacement bus that skips most
    stops of the rail line)."""
    hops: list[tuple[str, str, Coordinates | None]] = []
    i = 0
    while i < len(station_ids) - 1:
        a, b = station_ids[i], station_ids[i + 1]
        segment = get_segment(conn, a, b) if conn else None
        if segment is None and conn:
            opposite = get_segment(conn, b, a)
            segment = opposite[::-1] if opposite else None
        end = i + 1
        if segment is None and conn:
            for j in range(i + 2, len(station_ids)):
                spanning = get_segment(conn, a, station_ids[j])
                if spanning is None:
                    opposite = get_segment(conn, station_ids[j], a)
                    spanning = opposite[::-1] if opposite else None
                if spanning is not None:
                    segment, b, end = spanning, station_ids[j], j
                    break
        hops.append((a, b, segment))
        i = end
    return hops


REAL_TRACK_MAX_HOP_M = 6000.0  # longer point gaps are straight links, not track


def is_real_track(segment: Coordinates | None) -> bool:
    """True for a stored track: more than two points, no long straight gap."""
    if not segment or len(segment) <= 2:
        return False
    return all(
        haversine(a[0], a[1], b[0], b[1]) <= REAL_TRACK_MAX_HOP_M
        for a, b in zip(segment, segment[1:])
    )


def store_segment(
    conn: sqlite3.Connection, from_id: str, to_id: str, coordinates: Coordinates
) -> None:
    existing = get_segment(conn, from_id, to_id)
    if is_real_track(existing) and not is_real_track(coordinates):
        return  # keep a real track over a straight-line fallback
    conn.execute(
        "INSERT OR REPLACE INTO segments (from_id, to_id, coordinates_json, last_seen) "
        "VALUES (?, ?, ?, ?)",
        (from_id, to_id, json.dumps(coordinates), int(time.time())),
    )
    conn.commit()


def is_resolved(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM subline_schedule WHERE subline_key = ?", (key,)
    ).fetchone()
    return row is not None


def load_schedule(conn: sqlite3.Connection, key: str) -> list[dict] | None:
    row = conn.execute(
        "SELECT stop_offsets_json FROM subline_schedule WHERE subline_key = ?", (key,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def store_schedule(
    conn: sqlite3.Connection, key: str, service_id, stop_offsets: list[dict]
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO subline_schedule "
        "(subline_key, service_id, stop_offsets_json, resolved_at) VALUES (?, ?, ?, ?)",
        (key, str(service_id), json.dumps(stop_offsets), int(time.time())),
    )
    conn.commit()


def log_course(
    conn: sqlite3.Connection,
    run_id: int,
    key: str,
    line_name: str,
    service_id: str,
    info: dict,
    result: str,
    segments: int,
) -> None:
    """One row per departureCourse attempt (result "ok" or the error kind)."""
    conn.execute(
        "INSERT INTO course_log (run_id, called_at, subline_key, line_name, "
        "service_id, station, dep_time, result, segments, delays_json, meta_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            int(time.time()),
            key,
            line_name,
            str(service_id),
            info.get("station"),
            info.get("dep_time"),
            result,
            segments,
            json.dumps(info.get("legs") or []),
            json.dumps(info.get("meta") or {}),
        ),
    )
    conn.commit()


def _max_delay(legs: list[dict]) -> int | None:
    values = [
        leg[key]
        for leg in legs
        for key in ("dep_delay", "arr_delay")
        if leg.get(key) is not None
    ]
    return max(values) if values else None


def read_course_log(conn: sqlite3.Connection | None, limit: int = 500) -> dict:
    """Overview for the courses debug page: the latest attempts and per-run
    totals. Empty if the log table doesn't exist yet."""
    empty = {"calls": [], "runs": []}
    if conn is None:
        return empty
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(course_log)")}
        meta_column = "meta_json" if "meta_json" in columns else "NULL"
        rows = conn.execute(
            "SELECT id, run_id, called_at, subline_key, line_name, service_id, "
            f"station, dep_time, result, segments, delays_json, {meta_column} "
            "FROM course_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        runs = conn.execute(
            "SELECT run_id, COUNT(*), SUM(result = 'ok'), MIN(called_at), "
            "MAX(called_at) FROM course_log GROUP BY run_id ORDER BY run_id DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return empty

    calls = []
    for row in rows:
        legs = json.loads(row[10] or "[]")
        calls.append(
            {
                "id": row[0],
                "run_id": row[1],
                "called_at": row[2],
                "subline_key": row[3],
                "line": row[4],
                "service_id": row[5],
                "station": row[6],
                "dep_time": row[7],
                "result": row[8],
                "segments": row[9],
                "max_delay_s": _max_delay(legs),
                "legs": legs,
                "meta": json.loads(row[11] or "{}"),
            }
        )
    return {
        "calls": calls,
        "runs": [
            {
                "run_id": r[0],
                "calls": r[1],
                "ok": r[2] or 0,
                "first": r[3],
                "last": r[4],
            }
            for r in runs
        ],
    }


def _entries_by_key(by_service: dict) -> dict[str, list[tuple[str, dict]]]:
    """Every (service_id, entry) per distinct resolved subline key."""
    grouped: dict[str, list[tuple[str, dict]]] = {}
    for service_id, entry in by_service.items():
        subline = entry.get("subline")
        if not subline:
            continue
        key = subline_key(entry["line"].get("id", ""), tuple(s["id"] for s in subline))
        grouped.setdefault(key, []).append((service_id, entry))
    return grouped


def _spread(items: list, count: int) -> list:
    """Up to count items evenly spread over the list."""
    if len(items) <= count:
        return list(items)
    step = (len(items) - 1) / (count - 1) if count > 1 else 0
    return [items[round(i * step)] for i in range(count)]


def _has_missing_hops(conn: sqlite3.Connection, subline: list[dict]) -> bool:
    ids = tuple(s["id"] for s in subline)
    return any(segment is None for _, _, segment in subline_hops(conn, ids))


def _departure_query(entry: dict, anchor_start: datetime) -> tuple[str, str] | None:
    """(station name, ISO depTime) for the entry's first stop - what
    departureCourse needs as its own station/time - or None if that stop is
    missing a name or offset."""
    stops = entry.get("stops") or []
    if not stops:
        return None
    first = stops[0]
    # departureList's own station object uses "combinedName", not "name"
    # (that's StationLight's field, from listLines/hvv:stations).
    station_name = (first.get("station") or {}).get("combinedName")
    offset = first.get("timeOffset")
    if not station_name or offset is None:
        return None
    dep_time = anchor_start + timedelta(minutes=offset)
    return station_name, dep_time.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def _extract_course(course_response: dict) -> tuple[list[tuple], list[dict]] | None:
    """From one departureCourse response: [(from_id, to_id, coordinates), ...]
    per element with a real track, and the per-stop schedule (station id +
    offset in seconds from the first element's depTime). None if the
    response has no courseElements (e.g. an unresolved/expired query)."""
    elements = course_response.get("courseElements") or []
    if not elements:
        return None

    first_dep = datetime.fromisoformat(elements[0]["depTime"])
    segments = []
    stop_offsets = [
        {
            "id": elements[0]["fromStation"]["id"],
            "name": elements[0]["fromStation"]["name"],
            "offset": 0,
        }
    ]
    for element in elements:
        from_station, to_station = element["fromStation"], element["toStation"]
        track = (element.get("path") or {}).get("track") or []
        if track:
            coordinates = [[p["x"], p["y"]] for p in track]
        else:
            from_c, to_c = from_station["coordinate"], to_station["coordinate"]
            coordinates = [[from_c["x"], from_c["y"]], [to_c["x"], to_c["y"]]]
        segments.append((from_station["id"], to_station["id"], coordinates))

        arr_time = datetime.fromisoformat(element["arrTime"])
        stop_offsets.append(
            {
                "id": to_station["id"],
                "name": to_station["name"],
                "offset": round((arr_time - first_dep).total_seconds()),
            }
        )
    return segments, stop_offsets


_LEG_KEYS = {
    "fromStation", "toStation", "path", "depTime", "arrTime", "depDelay", "arrDelay"
}  # fmt: skip


def _course_legs(course_response: dict) -> list[dict]:
    """Per course element the stations, times and delays (seconds) as the API
    gives them, None where absent (not every operator reports delays);
    "extra" carries every other field (platforms, model, ...)."""
    return [
        {
            "from": (e.get("fromStation") or {}).get("name"),
            "to": (e.get("toStation") or {}).get("name"),
            "dep_time": e.get("depTime"),
            "arr_time": e.get("arrTime"),
            "dep_delay": e.get("depDelay"),
            "arr_delay": e.get("arrDelay"),
            "extra": {k: v for k, v in e.items() if k not in _LEG_KEYS},
        }
        for e in course_response.get("courseElements") or []
    ]


def _course_meta(course_response: dict) -> dict:
    """Top-level response fields besides the course elements themselves."""
    return {k: v for k, v in course_response.items() if k != "courseElements"}


def _concat_coordinates(coordinate_lists: list[Coordinates]) -> Coordinates:
    """Join consecutive segments' coordinates into one path, dropping the
    duplicate point at each junction."""
    result = list(coordinate_lists[0])
    for coordinates in coordinate_lists[1:]:
        result.extend(coordinates[1:] if coordinates[0] == result[-1] else coordinates)
    return result


def _declared_segment_bridges(
    subline: list[dict], segments: list[tuple[str, str, Coordinates]]
) -> list[tuple[str, str, Coordinates]]:
    """Some sublines declare two stations as directly adjacent (per
    listLines' stationSequence) while the real, observed course actually
    stops at one or more extra stations in between (e.g. a variant listed as
    skipping a stop that every real trip still serves). Without this, the
    declared pair would never get a stored segment - gtilive_geojson's
    _subline_coordinates() falls back to a straight line for it even though
    the real geometry is already known, just filed under the intermediate
    stations' own pairs. Bridges each declared-adjacent pair by concatenating
    whichever real segments span it, in either direction."""
    if not segments:
        return []
    real_order = [segments[0][0]] + [seg[1] for seg in segments]
    index_of = {station_id: i for i, station_id in enumerate(real_order)}

    bridges = []
    for a, b in zip(subline, subline[1:]):
        i, j = index_of.get(a["id"]), index_of.get(b["id"])
        if i is None or j is None or i == j:
            continue
        if j > i:
            coordinates = _concat_coordinates([seg[2] for seg in segments[i:j]])
        else:
            coordinates = _concat_coordinates(
                [list(reversed(seg[2])) for seg in reversed(segments[j:i])]
            )
        bridges.append((a["id"], b["id"], coordinates))
    return bridges


def _load_anchor_start(redis_client) -> datetime | None:
    payload = redis_client.get(REDIS_ANCHOR_KEY)
    if not payload:
        return None
    data = json.loads(payload)["data"]
    naive = datetime.strptime(f"{data['date']} {data['time']}", "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=BERLIN_TZ)


def _load_by_service(redis_client) -> dict:
    payload = redis_client.get(REDIS_BY_SERVICE_KEY)
    return json.loads(payload)["data"] if payload else {}


def _query_course(
    client: GtiClient, service_id: str, entry: dict, anchor_start: datetime
) -> tuple[tuple | None, str, str, dict]:
    """departureCourse for one trip: (extracted course or None, error kind
    "no_query"/"api" or "", detail for the first-failure log, log info)."""
    query = _departure_query(entry, anchor_start)
    if query is None:
        detail = f"stops[0]={entry.get('stops', [None])[:1]!r}"
        return None, "no_query", detail, {}
    station_name, iso_time = query
    info = {"station": station_name, "dep_time": iso_time}
    request = {
        "station": {"type": "STATION", "name": station_name},
        "time": iso_time,
        # departureCourse wants "dlid" (e.g. "de:hvv:U1:"), not "id"
        # (the icon lineKey, e.g. "HHA-U:U1_HHA-U") - the latter is
        # only for matching against the sublines cache elsewhere.
        "lineId": entry["line"].get("dlid", ""),
        "serviceId": int(service_id),
        "showPath": True,
    }
    import requests  # lazy: the api image does not install it

    try:
        response = client.send("departureCourse", request)
    except requests.RequestException as error:
        time.sleep(1)
        return None, "api", f"request={request!r} error={error}", info
    time.sleep(1)  # 1 req/s rate limit across all GTI endpoints
    info["legs"] = _course_legs(response)
    info["meta"] = _course_meta(response)
    extracted = _extract_course(response)
    if extracted is None:
        return None, "api", f"request={request!r} response={response!r}", info
    return extracted, "", "", info


def update_gti_shapes(
    client: GtiClient, redis_client, conn: sqlite3.Connection
) -> dict:
    """For every resolved subline not yet in subline_schedule, query
    departureCourse once, and persist its segment geometry plus scheduled
    stop offsets. While a subline still has hops without a stored segment
    (trips of a variant may skip declared stops), up to EXTRA_COURSE_TRIES
    further trips are queried, also for already resolved sublines.
    Rate-limited to 1 req/s across all calls made. Returns a
    {"resolved": n, "skipped": n, "failed": n} summary."""
    by_service = _load_by_service(redis_client)
    anchor_start = _load_anchor_start(redis_client)
    if anchor_start is None:
        print("no hvv:departures:anchor_start cached - run hvvmap-departures first")
        return {"resolved": 0, "skipped": 0, "failed": 0}

    summary = {"resolved": 0, "skipped": 0, "failed": 0, "no_query": 0}
    run_id = int(time.time())
    for key, trips in _entries_by_key(by_service).items():
        resolved = is_resolved(conn, key)
        subline = trips[0][1].get("subline") or []
        if resolved and not _has_missing_hops(conn, subline):
            summary["skipped"] += 1
            continue
        if resolved:
            candidates = _spread(trips, EXTRA_COURSE_TRIES + 1)
        else:
            candidates = trips[:1] + _spread(trips[1:], EXTRA_COURSE_TRIES)
        error, detail = "", ""
        for service_id, entry in candidates:
            extracted, error, detail, info = _query_course(
                client, service_id, entry, anchor_start
            )
            log_course(
                conn,
                run_id,
                key,
                entry["line"].get("name", ""),
                service_id,
                info,
                "ok" if extracted else error,
                len(extracted[0]) if extracted else 0,
            )
            if extracted is None:
                continue
            segments, stop_offsets = extracted
            for from_id, to_id, coordinates in segments:
                store_segment(conn, from_id, to_id, coordinates)
            for from_id, to_id, coordinates in _declared_segment_bridges(
                subline, segments
            ):
                store_segment(conn, from_id, to_id, coordinates)
            if not resolved:
                store_schedule(conn, key, service_id, stop_offsets)
                resolved = True
                summary["resolved"] += 1
            if not _has_missing_hops(conn, subline):
                break
        if not resolved:
            if summary["failed"] == 0:
                print(f"first failure ({key}, {error}): {detail}")
            summary["failed"] += 1
            if error == "no_query":
                summary["no_query"] += 1
    return summary


DERIVED_MAX_PER_RUN = 20  # departureCourse calls for service variants per run


def variant_key(entry: dict) -> str:
    """Services of one line and destination that were observed at the same
    stations in the same order are taken to run the same variant."""
    ids: list[str] = []
    for stop in entry.get("stops") or []:
        station_id = (stop.get("station") or {}).get("id")
        if station_id and (not ids or ids[-1] != station_id):
            ids.append(station_id)
    return "|".join([entry["line"].get("id", ""), str(entry.get("destination")), *ids])


def _needs_course(entry: dict) -> bool:
    """No matched subline, or a replacement bus: its anchors alone cannot tell
    a direct run from one that serves every stop of the rail line."""
    return (
        not entry.get("subline")
        or (entry["line"].get("type") or {}).get("shortInfo") == "Bus"
    )


def store_variant_subline(
    conn: sqlite3.Connection, key: str, subline: list[dict]
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO variant_sublines (variant_key, subline_json, "
        "resolved_at) VALUES (?, ?, ?)",
        (key, json.dumps(subline), int(time.time())),
    )
    conn.commit()


def apply_derived_sublines(conn: sqlite3.Connection | None, by_service: dict) -> dict:
    """Replace the subline of services without one, and of replacement buses,
    by the station sequence of their variant's own departureCourse (see
    derive_unresolved_sublines)."""
    if conn is None:
        return by_service
    try:
        rows = dict(
            conn.execute("SELECT variant_key, subline_json FROM variant_sublines")
        )
    except sqlite3.OperationalError:
        return by_service
    for entry in by_service.values():
        if not _needs_course(entry):
            continue
        subline_json = rows.get(variant_key(entry))
        if subline_json:
            entry["subline"] = json.loads(subline_json)
    return by_service


def derive_unresolved_sublines(
    client: GtiClient,
    by_service: dict,
    anchor_start: datetime,
    conn: sqlite3.Connection,
) -> dict:
    """One departureCourse per variant (see variant_key) of the services that
    need one, for stations and track. At most DERIVED_MAX_PER_RUN calls;
    failed variants are tried again next run."""
    summary = {"derived": 0, "failed": 0, "remaining": 0}
    done = {row[0] for row in conn.execute("SELECT variant_key FROM variant_sublines")}
    run_id = int(time.time())
    seen: set[str] = set()
    for service_id, entry in by_service.items():
        key = variant_key(entry)
        if not _needs_course(entry) or key in done or key in seen:
            continue
        seen.add(key)
        if summary["derived"] + summary["failed"] >= DERIVED_MAX_PER_RUN:
            summary["remaining"] += 1
            continue
        extracted, error, _, info = _query_course(
            client, service_id, entry, anchor_start
        )
        log_course(
            conn,
            run_id,
            "derived",
            entry["line"].get("name", ""),
            service_id,
            info,
            "ok" if extracted else error,
            len(extracted[0]) if extracted else 0,
        )
        if extracted is None:
            summary["failed"] += 1
            continue
        segments, stop_offsets = extracted
        for from_id, to_id, coordinates in segments:
            store_segment(conn, from_id, to_id, coordinates)
        store_variant_subline(
            conn,
            key,
            [{"id": stop["id"], "name": stop["name"]} for stop in stop_offsets],
        )
        summary["derived"] += 1
    return summary


def run_update(
    client: GtiClient, redis_client, conn: sqlite3.Connection
) -> tuple[dict, dict | None]:
    """Resolve new sublines and derive variant sublines; returns both
    summaries (the second is None without a cached anchor)."""
    summary = update_gti_shapes(client, redis_client, conn)
    anchor_start = _load_anchor_start(redis_client)
    if anchor_start is None:
        return summary, None
    derived = derive_unresolved_sublines(
        client, _load_by_service(redis_client), anchor_start, conn
    )
    return summary, derived


def main() -> None:
    """CLI: resolve every not-yet-resolved gti-live subline via
    departureCourse."""
    from hvv_map.gti_client import GtiClient
    from hvv_map.redis_client import get_redis_client

    summary, derived = run_update(GtiClient(), get_redis_client(), open_db())
    print(
        f"{summary['resolved']} newly resolved, {summary['skipped']} already "
        f"resolved, {summary['failed']} failed ({summary['no_query']} of those "
        "had no usable station/time before any API call was made)"
    )
    if derived:
        print(
            f"{derived['derived']} service variants got a subline from their "
            f"course, {derived['failed']} failed, {derived['remaining']} still waiting"
        )


if __name__ == "__main__":
    main()
