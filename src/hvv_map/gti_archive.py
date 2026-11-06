"""Archive of the GTI departureList and departureCourse answers in SQLite,
in the live.sqlite format of ubahnkarte (tables departures, courses and
queries, same columns and upserts), for later analysis. Write-only for now;
a failure here never stops a fetch.

The file sits next to GTI_SHAPES_DB_PATH unless GTI_ARCHIVE_DB_PATH is set;
an empty GTI_ARCHIVE_DB_PATH disables the archive."""

import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
DAY_BOUNDARY = timedelta(hours=3)  # departures before 03:00: previous day
KEEP_DAYS = int(os.environ.get("GTI_ARCHIVE_KEEP_DAYS", "4"))
PURGE_HOUR = 4
BUSY_TIMEOUT_S = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS departures (
    operating_day     TEXT    NOT NULL,
    line_key          TEXT    NOT NULL,
    service_id        INTEGER NOT NULL,
    station_id        TEXT    NOT NULL,
    line_name         TEXT,
    origin            TEXT,
    direction         TEXT,
    direction_id      INTEGER,
    dep_time          TEXT    NOT NULL,
    delay_s           INTEGER,
    platform          TEXT,
    realtime_platform TEXT,
    cancelled         INTEGER NOT NULL DEFAULT 0,
    extra             INTEGER NOT NULL DEFAULT 0,
    vehicle_prefix    TEXT,
    vehicles          TEXT,
    first_direction   TEXT,
    first_platform    TEXT,
    raw               TEXT    NOT NULL,
    raw_hash          TEXT    NOT NULL,
    first_seen_at     TEXT    NOT NULL,
    last_seen_at      TEXT    NOT NULL,
    last_changed_at   TEXT    NOT NULL,
    seen_count        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (operating_day, line_key, service_id, station_id)
);
CREATE INDEX IF NOT EXISTS departures_prefix
    ON departures (operating_day, vehicle_prefix);
CREATE INDEX IF NOT EXISTS departures_station_time
    ON departures (station_id, dep_time);

CREATE TABLE IF NOT EXISTS courses (
    operating_day    TEXT    NOT NULL,
    line_key         TEXT    NOT NULL,
    service_id       INTEGER NOT NULL,
    line_name        TEXT,
    origin           TEXT,
    direction        TEXT,
    first_stop_id    TEXT,
    first_dep_time   TEXT,
    last_stop_id     TEXT,
    last_arr_time    TEXT,
    vehicle_prefix   TEXT,
    vehicles         TEXT,
    model            TEXT,
    any_cancelled    INTEGER,
    stop_seq_hash    TEXT,
    request          TEXT    NOT NULL,
    response         TEXT    NOT NULL,
    response_hash    TEXT    NOT NULL,
    trigger          TEXT,
    first_fetched_at TEXT    NOT NULL,
    last_updated_at  TEXT    NOT NULL,
    last_changed_at  TEXT    NOT NULL,
    fetch_count      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (operating_day, line_key, service_id)
);

CREATE TABLE IF NOT EXISTS queries (
    id           INTEGER PRIMARY KEY,
    endpoint     TEXT    NOT NULL,
    group_name   TEXT,
    sent_at      TEXT    NOT NULL,
    query_time   TEXT,
    status       INTEGER NOT NULL,
    duration_s   REAL,
    n_departures INTEGER,
    error        TEXT,
    window_min   INTEGER
);
CREATE INDEX IF NOT EXISTS queries_sent ON queries (sent_at);
"""

# FzgID_<Zugnummer>_<day of month>_<unit>, see departures_tree.train_number
FZG = re.compile(r"^FzgID_(\d{6}_\d{1,2})_\d+$")
# GTI adds k * NEXT_DAY_OFFSET to plan IDs for queries dated off the trip's day
NEXT_DAY_OFFSET = 100_000_000
PLAN_ID_MAX = 100_000
# A departure this soon after the trip's last one on the previous operating
# day continues that trip (night service across 03:00).
CONTINUATION = timedelta(minutes=20)
LINE_KEY = re.compile(r":([^_:]+)_")


def default_path() -> str:
    shapes = os.environ.get("GTI_SHAPES_DB_PATH", "gti_shapes.db")
    return os.path.join(os.path.dirname(shapes), "gti_live.sqlite")


_conn: sqlite3.Connection | None = None
_last_purge_day: str | None = None


def connection() -> sqlite3.Connection | None:
    """Shared connection, None if the archive is disabled or unavailable."""
    global _conn
    if _conn is None:
        path = os.environ.get("GTI_ARCHIVE_DB_PATH", default_path())
        if not path:
            return None
        try:
            _conn = connect(path)
        except sqlite3.Error as error:
            print(f"[gti_archive] {path}: {error}", flush=True)
            return None
    return _conn


def connect(path: str) -> sqlite3.Connection:
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(datetime.now(BERLIN).replace(microsecond=0))


def operating_day(moment: datetime) -> str:
    return (moment.astimezone(BERLIN) - DAY_BOUNDARY).date().isoformat()


def list_base_time(response_time: dict) -> datetime:
    """'time' block of a departureList response as aware datetime."""
    return datetime.strptime(
        f"{response_time['date']} {response_time['time']}", "%d.%m.%Y %H:%M"
    ).replace(tzinfo=BERLIN)


def parse_gti_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def line_from_key(line_key: str) -> str | None:
    """'HHA-U:U1_HHA-U' -> 'U1'."""
    match = LINE_KEY.search(line_key or "")
    return match.group(1) if match else None


def normalize_service_id(service_id: int) -> int:
    if (
        NEXT_DAY_OFFSET <= service_id < 10 * NEXT_DAY_OFFSET
        and service_id % NEXT_DAY_OFFSET < PLAN_ID_MAX
    ):
        return service_id % NEXT_DAY_OFFSET
    return service_id


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _hash(obj) -> str:
    return hashlib.sha1(_dumps(obj).encode("utf-8")).hexdigest()


def vehicle_info(vehicles: list | None) -> tuple:
    """Sorted unit numbers (JSON) and FzgID prefix (Zugnummer_day)."""
    if not vehicles:
        return None, None
    numbers = sorted(str(v.get("number", "")) for v in vehicles)
    prefixes = sorted(
        {m.group(1) for v in vehicles if (m := FZG.match(str(v.get("id", ""))))}
    )
    return _dumps(numbers), (prefixes[0] if prefixes else None)


# --- departures -------------------------------------------------------------


def departure_row(dep: dict, response_time: dict, seen_at: str) -> dict:
    dep_time = list_base_time(response_time) + timedelta(minutes=dep["timeOffset"])
    raw = dict(dep)
    if raw.get("vehicles"):
        raw["vehicles"] = sorted(raw["vehicles"], key=lambda v: str(v.get("id")))
    vehicles, prefix = vehicle_info(dep.get("vehicles"))
    line = dep["line"]
    return {
        "operating_day": operating_day(dep_time),
        "line_key": line["id"],
        "service_id": normalize_service_id(dep["serviceId"]),
        "station_id": dep["station"]["id"],
        "line_name": line.get("name"),
        "origin": line.get("origin"),
        "direction": line.get("direction"),
        "direction_id": dep.get("directionId"),
        "dep_time": iso(dep_time),
        "delay_s": dep.get("delay"),
        "platform": dep.get("platform"),
        "realtime_platform": dep.get("realtimePlatform"),
        "cancelled": int(bool(dep.get("cancelled"))),
        "extra": int(bool(dep.get("extra"))),
        "vehicle_prefix": prefix,
        "vehicles": vehicles,
        "first_direction": line.get("direction"),
        "first_platform": dep.get("realtimePlatform"),
        "raw": _dumps(raw),
        "raw_hash": _hash(raw),
        "first_seen_at": seen_at,
        "last_seen_at": seen_at,
        "last_changed_at": seen_at,
    }


DEPARTURE_COLUMNS = [
    "operating_day",
    "line_key",
    "service_id",
    "station_id",
    "line_name",
    "origin",
    "direction",
    "direction_id",
    "dep_time",
    "delay_s",
    "platform",
    "realtime_platform",
    "cancelled",
    "extra",
    "vehicle_prefix",
    "vehicles",
    "first_direction",
    "first_platform",
    "raw",
    "raw_hash",
    "first_seen_at",
    "last_seen_at",
    "last_changed_at",
]

# first_* columns and first_seen_at are kept; older states never overwrite.
UPSERT_DEPARTURE = f"""
INSERT INTO departures ({", ".join(DEPARTURE_COLUMNS)})
VALUES ({", ".join(":" + c for c in DEPARTURE_COLUMNS)})
ON CONFLICT (operating_day, line_key, service_id, station_id) DO UPDATE SET
    line_name = excluded.line_name,
    origin = excluded.origin,
    direction = excluded.direction,
    direction_id = excluded.direction_id,
    dep_time = excluded.dep_time,
    delay_s = excluded.delay_s,
    platform = excluded.platform,
    realtime_platform = excluded.realtime_platform,
    cancelled = excluded.cancelled,
    extra = excluded.extra,
    vehicle_prefix = excluded.vehicle_prefix,
    vehicles = excluded.vehicles,
    raw = excluded.raw,
    last_changed_at = CASE
        WHEN departures.raw_hash != excluded.raw_hash
        THEN excluded.last_seen_at ELSE departures.last_changed_at END,
    raw_hash = excluded.raw_hash,
    last_seen_at = excluded.last_seen_at,
    seen_count = departures.seen_count + 1
WHERE excluded.last_seen_at >= departures.last_seen_at
"""


# GTI reuses some plan IDs for a second trip on the same day (U1/U2, e.g.
# 22141 at 04:50 and 15:09 on 05.11.). Later runs are stored as
# -(run * RUN_BASE + id), as in ubahnkarte.
RUN_BASE = PLAN_ID_MAX
RUN_GAP = timedelta(hours=2)  # farther from every run: a new one
MAX_RUNS = 10
SAME_PLAN_ID = "service_id IN ({})".format(
    ", ".join(["(:sid)"] + [f"(-{r} * :base - :sid)" for r in range(1, MAX_RUNS)])
)


def run_service_id(service_id: int, run: int) -> int:
    return service_id if run == 0 else -(run * RUN_BASE + service_id)


def split_run(service_id: int) -> tuple:
    """(GTI service ID, run) of a stored service_id."""
    if service_id >= 0:
        return service_id, 0
    return -service_id % RUN_BASE, -service_id // RUN_BASE


def _runs(conn, day, line_key, sid):
    """[(stored service_id, first, last departure, Zugnummer prefixes)] of a
    plan ID's runs."""
    return [
        (
            r[0],
            datetime.fromisoformat(r[1]),
            datetime.fromisoformat(r[2]),
            set((r[3] or "").split(",")) - {""},
        )
        for r in conn.execute(
            "SELECT service_id, min(dep_time), max(dep_time),"
            " group_concat(DISTINCT vehicle_prefix) FROM departures"
            f" WHERE operating_day = :day AND line_key = :lk AND {SAME_PLAN_ID}"
            " GROUP BY service_id",
            {"day": day, "lk": line_key, "sid": sid, "base": RUN_BASE},
        )
    ]


def _nearest_run(runs, at, prefix=None):
    """Nearest run within RUN_GAP; a different Zugnummer is another run even
    when close (21914 on 06.11.: 010821 until 09:10, 340925 from 09:25)."""
    best = None
    for sid, first, last, prefixes in runs:
        if prefix and prefixes and prefix not in prefixes:
            continue
        gap = max(first - at, at - last, timedelta(0))
        if gap <= RUN_GAP and (best is None or gap < best[0]):
            best = (gap, sid)
    return best[1] if best else None


def assign_run(conn, row: dict):
    """Set a departure's service_id to the run of its plan ID it belongs to,
    or to a new run when it is far from all of them."""
    sid = row["service_id"]
    if not 0 <= sid < PLAN_ID_MAX:
        return
    runs = _runs(conn, row["operating_day"], row["line_key"], sid)
    if not runs:
        return
    found = _nearest_run(
        runs, datetime.fromisoformat(row["dep_time"]), row.get("vehicle_prefix")
    )
    if found is None:
        run = max(split_run(r[0])[1] for r in runs) + 1
        if run >= MAX_RUNS:
            return
        found = run_service_id(sid, run)
    row["service_id"] = found


def assign_course_run(conn, row: dict):
    """A course's run: the one its first departure falls into."""
    sid = row["service_id"]
    if not 0 <= sid < PLAN_ID_MAX:
        return
    runs = _runs(conn, row["operating_day"], row["line_key"], sid)
    found = _nearest_run(
        runs, parse_gti_datetime(row["first_dep_time"]), row.get("vehicle_prefix")
    )
    if found is not None:
        row["service_id"] = found


def continue_trip(conn, row: dict):
    """Assign a departure to the previous operating day if its trip runs
    there until shortly before (night trips crossing 03:00)."""
    dep_time = datetime.fromisoformat(row["dep_time"])
    if not 3 <= dep_time.hour < 6:
        return
    prev = (date.fromisoformat(row["operating_day"]) - timedelta(days=1)).isoformat()
    last = conn.execute(
        "SELECT max(dep_time) FROM departures WHERE operating_day = :day"
        f" AND line_key = :lk AND {SAME_PLAN_ID}",
        {
            "day": prev,
            "lk": row["line_key"],
            "sid": row["service_id"],
            "base": RUN_BASE,
        },
    ).fetchone()[0]
    if last and dep_time - datetime.fromisoformat(last) <= CONTINUATION:
        row["operating_day"] = prev


def log_query(conn, **fields):
    keys = list(fields)
    conn.execute(
        f"INSERT INTO queries ({', '.join(keys)})"
        f" VALUES ({', '.join('?' for _ in keys)})",
        [fields[k] for k in keys],
    )


# --- courses ----------------------------------------------------------------


def stop_seq_hash(station_ids: list) -> str:
    return hashlib.sha1("|".join(station_ids).encode()).hexdigest()[:16]


def course_row(
    request: dict, response: dict, line: dict, fetched_at: str, trigger
) -> dict | None:
    """Row for a course; line is the departureList line (its id is the
    line_key, as in departures)."""
    elements = response.get("courseElements") or []
    if not elements or not line.get("id"):
        return None
    stations = [elements[0]["fromStation"]] + [e["toStation"] for e in elements]
    first_dep = parse_gti_datetime(elements[0]["depTime"])
    vehicles, prefix = vehicle_info(response.get("vehicles"))
    return {
        "operating_day": operating_day(first_dep),
        "line_key": line["id"],
        "service_id": normalize_service_id(request["serviceId"]),
        "line_name": line.get("name") or line_from_key(line["id"]),
        "origin": line.get("origin"),
        "direction": line.get("direction"),
        "first_stop_id": stations[0]["id"],
        "first_dep_time": elements[0]["depTime"],
        "last_stop_id": stations[-1]["id"],
        "last_arr_time": elements[-1]["arrTime"],
        "vehicle_prefix": prefix,
        "vehicles": vehicles,
        "model": elements[0].get("model"),
        "any_cancelled": int(
            any(e.get("fromCancelled") or e.get("toCancelled") for e in elements)
        ),
        "stop_seq_hash": stop_seq_hash([s["id"] for s in stations]),
        "request": _dumps(request),
        "response": _dumps(response),
        "response_hash": _hash(response),
        "trigger": trigger,
        "first_fetched_at": fetched_at,
        "last_updated_at": fetched_at,
        "last_changed_at": fetched_at,
    }


COURSE_COLUMNS = [
    "operating_day",
    "line_key",
    "service_id",
    "line_name",
    "origin",
    "direction",
    "first_stop_id",
    "first_dep_time",
    "last_stop_id",
    "last_arr_time",
    "vehicle_prefix",
    "vehicles",
    "model",
    "any_cancelled",
    "stop_seq_hash",
    "request",
    "response",
    "response_hash",
    "trigger",
    "first_fetched_at",
    "last_updated_at",
    "last_changed_at",
]

UPSERT_COURSE = f"""
INSERT INTO courses ({", ".join(COURSE_COLUMNS)})
VALUES ({", ".join(":" + c for c in COURSE_COLUMNS)})
ON CONFLICT (operating_day, line_key, service_id) DO UPDATE SET
    line_name = coalesce(excluded.line_name, courses.line_name),
    origin = excluded.origin,
    direction = excluded.direction,
    first_stop_id = excluded.first_stop_id,
    first_dep_time = excluded.first_dep_time,
    last_stop_id = excluded.last_stop_id,
    last_arr_time = excluded.last_arr_time,
    vehicle_prefix = excluded.vehicle_prefix,
    vehicles = excluded.vehicles,
    model = excluded.model,
    any_cancelled = excluded.any_cancelled,
    stop_seq_hash = excluded.stop_seq_hash,
    request = excluded.request,
    response = excluded.response,
    trigger = excluded.trigger,
    last_changed_at = CASE
        WHEN courses.response_hash != excluded.response_hash
        THEN excluded.last_updated_at ELSE courses.last_changed_at END,
    response_hash = excluded.response_hash,
    last_updated_at = excluded.last_updated_at,
    fetch_count = courses.fetch_count + 1
WHERE excluded.last_updated_at >= courses.last_updated_at
"""


def purge(conn, keep_days: int, today: str):
    """Delete data older than keep_days operating days."""
    cutoff = (date.fromisoformat(today) - timedelta(days=keep_days)).isoformat()
    conn.execute("DELETE FROM departures WHERE operating_day < ?", (cutoff,))
    conn.execute("DELETE FROM courses WHERE operating_day < ?", (cutoff,))
    conn.execute("DELETE FROM queries WHERE sent_at < ?", (cutoff,))
    conn.commit()


# --- entry points for the fetchers -------------------------------------------


def _safely(action):
    conn = connection()
    if conn is None:
        return
    try:
        action(conn)
        conn.commit()
    except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
        conn.rollback()
        print(f"[gti_archive] {error}", flush=True)


def record_query(**fields):
    """One GTI call, columns of the queries table."""
    _safely(lambda conn: log_query(conn, **fields))


def record_departures(response: dict, departures: list, seen_at: str):
    """Upsert departures of one departureList response."""

    def store(conn):
        for dep in departures:
            if dep.get("timeOffset") is None or not (dep.get("line") or {}).get("id"):
                continue
            row = departure_row(dep, response["time"], seen_at)
            continue_trip(conn, row)
            assign_run(conn, row)
            conn.execute(UPSERT_DEPARTURE, row)

    if response.get("time"):
        _safely(store)


def record_course(
    request: dict, response: dict, line: dict, fetched_at: str, trigger: str
):
    """Upsert a course with elements; line is its departureList line."""
    row = course_row(request, response, line, fetched_at, trigger)
    if row:

        def store(conn):
            assign_course_run(conn, row)
            conn.execute(UPSERT_COURSE, row)

        _safely(store)


def purge_if_due():
    """Once a day after PURGE_HOUR."""
    global _last_purge_day
    moment = datetime.now(BERLIN)
    today = moment.date().isoformat()
    if _last_purge_day == today or moment.hour < PURGE_HOUR:
        return
    _last_purge_day = today
    _safely(lambda conn: purge(conn, KEEP_DAYS, today))
