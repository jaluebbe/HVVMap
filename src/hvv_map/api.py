"""HVVMap web API.

Serves two independent things, read-only:
- a background vector basemap from a single .mbtiles file mounted under
  OSM_DIR (any filename - no region switching, HVVMap only ever needs one)
- the live HVV GeoJSON layers, built and cached in Redis by the separate
  hvvmap-fetcher process - pure passthrough, no computation here

Needs only Redis and the mounted .mbtiles file - no GTI credentials, no
segment_cache.
"""

import json
import mimetypes
import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from hvv_map.redis_client import get_redis_client

mimetypes.add_type("text/javascript", ".cjs")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static", html=True), name="static")
app.mount("/fonts", StaticFiles(directory="fonts"), name="fonts")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/static/index.html")


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


def _read_cached_json(key: str) -> dict:
    raw = _redis_client.get(key)
    if raw is None:
        raise HTTPException(
            status_code=503,
            detail=f"No data under Redis key '{key}' - is hvvmap-fetcher running?",
        )
    return json.loads(raw)["data"]


@app.get("/api/hvv/live/positions.geojson", tags=["hvv_live"])
def get_live_positions():
    return _read_cached_json("hvv:positions")


@app.get("/api/hvv/realtime/positions.geojson", tags=["hvv_realtime"])
def get_realtime_positions():
    return _read_cached_json("hvv:positions_realtime")


@app.get("/api/hvv/live/disruptions.geojson", tags=["hvv_live"])
def get_live_disruptions():
    return _read_cached_json("hvv:disruptions")


@app.get("/api/hvv/live/announcements.json", tags=["hvv_live"])
def get_live_announcements():
    """Raw getAnnouncements response, not GeoJSON - see hvv:disruptions for
    the derived, map-ready version."""
    return _read_cached_json("hvv:announcements")


@app.get("/api/hvv/live/stops.geojson", tags=["hvv_live"])
def get_live_stops():
    return _read_cached_json("hvv:reference_stops")


@app.get("/api/hvv/live/lines.geojson", tags=["hvv_live"])
def get_live_lines():
    return _read_cached_json("hvv:reference_lines")


# --- GTFS layers -------------------------------------------------------------
@app.get("/api/hvv/gtfs/positions.geojson", tags=["hvv_gtfs"])
def get_gtfs_positions():
    return _read_cached_json("hvv:gtfs:positions")


@app.get("/api/hvv/gtfs/lines.geojson", tags=["hvv_gtfs"])
def get_gtfs_lines():
    return _read_cached_json("hvv:gtfs:reference_lines")


@app.get("/api/hvv/gtfs/stops.geojson", tags=["hvv_gtfs"])
def get_gtfs_stops():
    return _read_cached_json("hvv:gtfs:reference_stops")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
