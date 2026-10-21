"""HVVMap web API.

Serves two independent things, read-only:
- a background vector basemap from a single .mbtiles file mounted under
  OSM_DIR (any filename - no region switching, HVVMap only ever needs one)
- the live HVV GeoJSON layers, built and cached in Redis by the separate
  hvvmap-fetcher process - pure passthrough, no computation here

Needs only Redis and the mounted .mbtiles file - no GTI credentials.
"""

import json
import mimetypes
import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from hvv_map.announcement_categories import classify_category
from hvv_map.departures_tree import build_station_tree, build_trip_tree, parse_anchor
from hvv_map.gti_shapes import DEFAULT_DB_PATH as GTI_SHAPES_DB_PATH
from hvv_map.gti_shapes import apply_derived_sublines, read_course_log
from hvv_map.gtilive_debug import build_debug_geojson
from hvv_map.gtilive_geojson import (
    build_lines_geojson as build_gtilive_lines_geojson,
)
from hvv_map.gtilive_geojson import (
    build_stops_geojson as build_gtilive_stops_geojson,
)
from hvv_map.redis_client import get_redis_client
from hvv_map.response_cache import CachedJsonResponse
from hvv_map.stations import by_id, load_stations
from hvv_map.trips import REDIS_VEHICLES_KEY, berlin_now, build_trip_table

mimetypes.add_type("text/javascript", ".cjs")

app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=1024)


class CachedStaticFiles(StaticFiles):
    """Long cache for bundled third-party assets; everything else is
    revalidated on each load so a deployment shows up immediately."""

    LONG_CACHE_PREFIXES = ("maplibre-gl/", "hvv/", "sprites/")

    def __init__(self, *args, long_cache: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.long_cache = long_cache

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            long_lived = self.long_cache or path.startswith(self.LONG_CACHE_PREFIXES)
            response.headers["Cache-Control"] = (
                "public, max-age=604800" if long_lived else "no-cache"
            )
        return response


app.mount("/static", CachedStaticFiles(directory="static", html=True), name="static")
app.mount("/fonts", CachedStaticFiles(directory="fonts", long_cache=True), name="fonts")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse("static/maplibre_map_hvv_gtilive.html")


@app.get("/robots.txt", include_in_schema=False)
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /api/\n")


# --- Background vector basemap ---------------------------------------------

OSM_DIR = Path("/osm")

# Persistent read-only connection, opened once and reused across requests.
# check_same_thread=False lets FastAPI's thread pool call it, but that alone
# doesn't make one connection safe under concurrent queries from different
# threads - the lock around each query prevents interleaved reads from
# returning spurious empty results under rapid tile requests.
_db_connection: sqlite3.Connection | None = None
_db_lock = threading.Lock()


def _basemap_path() -> Path | None:
    candidates = sorted(OSM_DIR.glob("*.mbtiles"))
    return candidates[0] if candidates else None


def _get_db_connection() -> sqlite3.Connection:
    global _db_connection
    if _db_connection is None:
        path = _basemap_path()
        if path is None:
            raise HTTPException(
                status_code=503, detail="No basemap .mbtiles file mounted under /osm"
            )
        _db_connection = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, check_same_thread=False
        )
        _db_connection.execute("PRAGMA cache_size = -8192")
        _db_connection.execute("PRAGMA mmap_size = 0")
        _db_connection.execute("PRAGMA temp_store = MEMORY")
    return _db_connection


def _base_url(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    port_suffix = f":{request.url.port}" if request.url.port else ""
    return f"{scheme}://{request.url.hostname}{port_suffix}"


@app.get("/api/vector/metadata.json", tags=["basemap"])
def get_vector_metadata(request: Request):
    conn = _get_db_connection()
    with _db_lock:
        rows = conn.execute("SELECT * FROM metadata").fetchall()
    base = _base_url(request)
    metadata = {
        "tilejson": "2.0.0",
        "scheme": "xyz",
        "tiles": [f"{base}/api/vector/tiles/{{z}}/{{x}}/{{y}}.pbf"],
    }
    for key, value in rows:
        if key == "json":
            metadata.update(json.loads(value))
        elif key in ("minzoom", "maxzoom"):
            metadata[key] = int(value)
        elif key in ("center", "bounds"):
            continue
        else:
            metadata[key] = value
    return metadata


@app.get("/api/vector/tiles/{zoom_level}/{x}/{y}.pbf", tags=["basemap"])
def get_vector_tiles(zoom_level: int, x: int, y: int):
    conn = _get_db_connection()
    tile_column = x
    tile_row = 2**zoom_level - 1 - y
    with _db_lock:
        result = conn.execute(
            "SELECT tile_data FROM tiles"
            " WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (zoom_level, tile_column, tile_row),
        ).fetchone()
    if result is None:
        raise HTTPException(status_code=404, detail="Tile not found.")
    return Response(
        content=result[0],
        media_type="application/octet-stream",
        headers={"Content-Encoding": "gzip"},
    )


@app.get("/api/vector/style/{style_name}.json", tags=["basemap"])
def get_vector_style(style_name: str, request: Request):
    style_file_name = f"{style_name}_style.json"
    if not Path(style_file_name).is_file():
        raise HTTPException(status_code=404, detail=f"Style '{style_name}' not known.")
    with open(style_file_name) as f:
        style = json.load(f)
    base = _base_url(request)
    vector_source_key = (
        "openmaptiles"
        if "openmaptiles" in style["sources"]
        else next(
            (
                k
                for k, v in style["sources"].items()
                if v.get("type") == "vector" and "url" in v
            ),
            None,
        )
    )
    if vector_source_key:
        style["sources"][vector_source_key]["url"] = f"{base}/api/vector/metadata.json"
    style["glyphs"] = f"{base}/fonts/{{fontstack}}/{{range}}.pbf"
    if style.get("sprite") is not None:
        style["sprite"] = f"{base}/static/sprites/{style_name}"
    return style


# --- Live HVV layers ---------------------------------------------------------

_redis_client = get_redis_client()

SUBLINES_REDIS_KEY = "hvv:sublines"
# Kept as a plain string (not imported from departures.py): that module pulls
# in gti_client's requests dependency, which the api image doesn't install.
REDIS_BY_SERVICE_KEY = "hvv:departures:by_service"
REDIS_MERGED_KEY = "hvv:departures:merged"
REDIS_ANCHOR_KEY = "hvv:departures:anchor_start"
REDIS_LIVE_KEY = "hvv:departures:live"
REDIS_TRIP_COURSES_KEY = "hvv:trips:courses"  # written by trip_courses.py
REDIS_POSITIONS_KEY = "hvv:positions_gtilive"  # written by trip_positions.py


def _read_cached_payload(key: str) -> dict:
    """The full {"fetched_at", "data"} payload stored under key."""
    raw = _redis_client.get(key)
    if raw is None:
        raise HTTPException(
            status_code=503,
            detail=f"No data under Redis key '{key}' - is hvvmap-fetcher running?",
        )
    return json.loads(raw)


def _read_cached_json(key: str) -> dict:
    return _read_cached_payload(key)["data"]


def _read_sublines_cache() -> dict:
    """hvv:sublines has its own payload shape (data_release_id alongside the
    lines dict, no "data" wrapper) - kept separate from _read_cached_json
    rather than forcing a shared shape on an unrelated cache."""
    raw = _redis_client.get(SUBLINES_REDIS_KEY)
    if raw is None:
        raise HTTPException(
            status_code=503,
            detail=f"No data under Redis key '{SUBLINES_REDIS_KEY}' - "
            "is hvvmap-fetcher running?",
        )
    return json.loads(raw)["lines"]


@app.get("/api/hvv/live/disruptions.geojson", tags=["hvv_live"])
def get_live_disruptions():
    return _read_cached_json("hvv:disruptions")


@app.get("/api/hvv/live/announcements.json", tags=["hvv_live"])
def get_live_announcements():
    """Raw HVV announcements, not GeoJSON."""
    return _read_cached_json("hvv:announcements")


@app.get("/api/hvv/live/announcement-categories.json", tags=["hvv_live"])
def get_announcement_categories():
    """Our own SPERRUNG/BARRIEREFREIHEIT/SONSTIGE classification per
    announcement id - not an HVV API field."""
    announcements = _read_cached_json("hvv:announcements").get("announcements", [])
    return {a["id"]: classify_category(a) for a in announcements if "id" in a}


@app.get("/api/hvv/live/lines.json", tags=["hvv_live"])
def get_live_lines_catalog():
    """Lines currently known, sorted by name."""
    lines = _read_sublines_cache()
    catalog = [
        {"id": line_id, "name": entry["line_name"]} for line_id, entry in lines.items()
    ]
    catalog.sort(key=lambda line: line["name"])
    return catalog


@app.get("/api/hvv/live/sublines.json", tags=["hvv_live"])
def get_live_sublines():
    """All known sublines per line, with each stop's id and name."""
    lines = _read_sublines_cache()
    stations = by_id(load_stations(_redis_client))

    result = {}
    for line_id, entry in lines.items():
        sublines = []
        for sub in entry["sublines"]:
            stops = [
                {
                    "id": stop["id"],
                    "name": stations[stop["id"]].name
                    if stop["id"] in stations
                    else stop["name"],
                }
                for stop in sub["stations"]
            ]
            sublines.append({"vehicle_type": sub["vehicle_type"], "stations": stops})
        result[line_id] = {"line_name": entry["line_name"], "sublines": sublines}
    return result


# --- Departures debug trees (static/dev_departures.html) --------------------
# Snapshot of the daily departureList run (departures.py), grouped for
# browsing - not linked from any map page.


def _read_anchor():
    raw = _redis_client.get(REDIS_ANCHOR_KEY)
    return parse_anchor(json.loads(raw)["data"]) if raw else None


def _departures_meta(payload: dict, anchor) -> dict:
    return {
        "fetched_at": payload.get("fetched_at"),
        "anchor": anchor.strftime("%Y-%m-%d %H:%M") if anchor else None,
    }


@app.get("/api/hvv/live/departures-by-station.json", tags=["hvv_live"])
def get_departures_by_station():
    """Mode -> line -> station -> departures, from hvv:departures:merged."""
    payload = _read_cached_payload(REDIS_MERGED_KEY)
    anchor = _read_anchor()
    tree = build_station_tree(payload["data"].get("departures", []), anchor)
    return {**_departures_meta(payload, anchor), **tree}


@app.get("/api/hvv/live/departures-live.json", tags=["hvv_live"])
def get_departures_live():
    """Mode -> line -> station -> realtime departures (delay, model,
    vehicles), from hvv:departures:live."""
    payload = _read_cached_payload(REDIS_LIVE_KEY)
    anchor = parse_anchor(payload["data"].get("anchor"))
    tree = build_station_tree(payload["data"].get("departures", []), anchor)
    return {**_departures_meta(payload, anchor), **tree}


@app.get("/api/hvv/live/trips.json", tags=["hvv_live"])
def get_trips():
    """Planned U-Bahn trips (by_service) matched against the realtime
    departures, for static/dev_trips.html."""
    plan = _read_cached_json(REDIS_BY_SERVICE_KEY)
    live_payload = _read_cached_payload(REDIS_LIVE_KEY)
    raw_courses = _redis_client.get(REDIS_TRIP_COURSES_KEY)
    courses = json.loads(raw_courses)["data"] if raw_courses else {}
    raw_vehicles = _redis_client.get(REDIS_VEHICLES_KEY)
    memory = json.loads(raw_vehicles)["data"] if raw_vehicles else {}
    table = build_trip_table(
        plan, _read_anchor(), live_payload["data"], courses, memory, berlin_now()
    )
    return {"fetched_at": live_payload.get("fetched_at"), **table}


@app.get("/api/hvv/live/departures-by-trip.json", tags=["hvv_live"])
def get_departures_by_trip():
    """Mode -> line -> trip (serviceId) -> stops, from by_service."""
    payload = _read_cached_payload(REDIS_BY_SERVICE_KEY)
    anchor = _read_anchor()
    tree = build_trip_tree(payload["data"], anchor)
    return {**_departures_meta(payload, anchor), **tree}


# --- gti-live layers: departureList/sublines based ---------------------------
# Built on the fly from departures.py's cached grouping (hvv:departures:by_service)
# and hvv:stations - cheap enough, and by_service itself only changes once a day.


@app.get("/api/hvv/gtilive/positions.geojson", tags=["hvv_gtilive"])
def get_gtilive_positions():
    """Vehicle positions computed from the trip table (trip_positions.py)."""
    return _read_cached_json(REDIS_POSITIONS_KEY)


@app.get("/api/hvv/gtilive/stops.geojson", tags=["hvv_gtilive"])
def get_gtilive_stops(request: Request):
    return _gtilive_stops_cache.response(request)


_gti_shapes_connection: sqlite3.Connection | None = None


def _get_gti_shapes_connection() -> sqlite3.Connection | None:
    """Read-only; None (no real geometry, straight lines only) if the
    fetcher-side hvvmap-gti-shapes-update hasn't run yet - not an error."""
    global _gti_shapes_connection
    if _gti_shapes_connection is None and Path(GTI_SHAPES_DB_PATH).is_file():
        _gti_shapes_connection = sqlite3.connect(
            f"file:{GTI_SHAPES_DB_PATH}?mode=ro", uri=True, check_same_thread=False
        )
    return _gti_shapes_connection


def _by_service_with_derived() -> dict:
    """by_service with course-derived sublines for services listLines did not
    match (see gti_shapes.derive_unresolved_sublines)."""
    return apply_derived_sublines(
        _get_gti_shapes_connection(), _read_cached_json(REDIS_BY_SERVICE_KEY)
    )


def _build_gtilive_stops() -> dict:
    stations = by_id(load_stations(_redis_client))
    return build_gtilive_stops_geojson(_by_service_with_derived(), stations)


def _build_gtilive_lines() -> dict:
    stations = by_id(load_stations(_redis_client))
    return build_gtilive_lines_geojson(
        _by_service_with_derived(), stations, _get_gti_shapes_connection()
    )


_gtilive_stops_cache = CachedJsonResponse(_build_gtilive_stops)
_gtilive_lines_cache = CachedJsonResponse(_build_gtilive_lines)
_gtfs_lines_cache = CachedJsonResponse(
    lambda: _read_cached_json("hvv:gtfs:reference_lines")
)
_gtfs_stops_cache = CachedJsonResponse(
    lambda: _read_cached_json("hvv:gtfs:reference_stops")
)


@app.get("/api/hvv/gtilive/lines.geojson", tags=["hvv_gtilive"])
def get_gtilive_lines(request: Request):
    return _gtilive_lines_cache.response(request)


@app.get("/api/hvv/gtilive/debug.geojson", tags=["hvv_gtilive"])
def get_gtilive_debug():
    """gti-live lines and stops with debug properties, for
    static/dev_gtilive_map.html."""
    by_service = _by_service_with_derived()
    stations = by_id(load_stations(_redis_client))
    return build_debug_geojson(by_service, stations, _get_gti_shapes_connection())


@app.get("/api/hvv/gtilive/courses.json", tags=["hvv_gtilive"])
def get_gtilive_courses(limit: int = 500):
    """departureCourse call log from hvvmap-gti-shapes-update, for
    static/dev_courses.html."""
    return read_course_log(_get_gti_shapes_connection(), max(1, min(limit, 5000)))


# --- GTFS layers -------------------------------------------------------------
@app.get("/api/hvv/gtfs/positions.geojson", tags=["hvv_gtfs"])
def get_gtfs_positions():
    return _read_cached_json("hvv:gtfs:positions")


_gtfs_departures_cache: dict = {}  # fetched_at -> {(line, station): departures}


@app.get("/api/hvv/gtfs/departures.json", tags=["hvv_gtfs"])
def get_gtfs_departures(line: str, station: str):
    """Scheduled departures of today at one station and line from the static
    GTFS feed (static/dev_station_departures.html)."""
    payload = _read_cached_payload("hvv:gtfs:departures")
    stamp = payload.get("fetched_at")
    if stamp not in _gtfs_departures_cache:
        index = {}
        for mode in payload["data"]["modes"]:
            for line_node in mode["lines"]:
                for station_node in line_node["stations"]:
                    key = (line_node["name"], station_node["name"])
                    index.setdefault(key, []).extend(station_node["departures"])
        _gtfs_departures_cache.clear()
        _gtfs_departures_cache[stamp] = index
    return {
        "fetched_at": stamp,
        "anchor": payload["data"]["anchor"],
        "departures": sorted(
            _gtfs_departures_cache[stamp].get((line, station), []),
            key=lambda d: d["offset"],
        ),
    }


@app.get("/api/hvv/gtfs/lines.geojson", tags=["hvv_gtfs"])
def get_gtfs_lines(request: Request):
    return _gtfs_lines_cache.response(request)


@app.get("/api/hvv/gtfs/stops.geojson", tags=["hvv_gtfs"])
def get_gtfs_stops(request: Request):
    return _gtfs_stops_cache.response(request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
