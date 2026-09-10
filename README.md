# HVVMap

Live and schedule-based HVV transit data (U-Bahn, S-Bahn, AKN, ferry) for
Hamburg, shown on a web map.

## Architecture

Three independent deployables sharing one Python package (`hvv_map`) and
one Redis instance:

- **fetcher** (`hvv_map.fetcher`) - background loop polling the HVV GTI API
  (`getVehicleMap`, `getAnnouncements`, `listLines`, `listStations`),
  respecting its 1 req/s rate limit. Writes GeoJSON layers to Redis:
  `hvv:positions`, `hvv:positions_realtime`, `hvv:disruptions`,
  `hvv:reference_lines`, `hvv:reference_stops`. Needs GTI credentials, no
  web-facing port.
- **gtfs-fetcher** (`hvv_map.gtfs_fetcher`) - background loop building the
  same kind of layers purely from a static GTFS feed on disk - no GTI
  credentials, no live API, no disruptions (a static feed has none). Writes
  `hvv:gtfs:positions`, `hvv:gtfs:reference_lines`, `hvv:gtfs:reference_stops`.
- **api** (`hvv_map.api`) - FastAPI app serving all of the above under
  `/api/hvv/live/*`, `/api/hvv/realtime/*` and `/api/hvv/gtfs/*`, plus a
  background vector basemap (a single `.mbtiles` file) and the static
  frontend. Needs only Redis, no GTI credentials, no GTFS feed.

Supporting modules (all under `src/hvv_map/`):

| Module | Purpose |
|---|---|
| `gti_client.py` | GTI API HTTP client (signing, credentials) |
| `redis_client.py` | shared Redis connection helper |
| `lines.py` / `stations.py` | GTI line/station reference data |
| `segment_cache.py` | SQLite cache of observed segment geometry, growing organically as the fetcher runs |
| `positions.py` | interpolation math (position along a segment at a given time) |
| `geojson.py` | builds `hvv:positions`/`hvv:positions_realtime` from a `getVehicleMap` response |
| `disruptions.py` | builds `hvv:disruptions` from a `getAnnouncements` response |
| `reference_geojson.py` | builds `hvv:reference_lines`/`hvv:reference_stops` from `segment_cache` + station data |
| `gtfs_schedule.py` | parses a static GTFS feed into an in-memory schedule, locates a trip's position at a given time |
| `gtfs_geojson.py` | builds the three `hvv:gtfs:*` layers from a loaded `Schedule` |

## Development

No `requirements.txt` - dependencies live in `pyproject.toml`, grouped into
optional extras so each deployable only pulls in what it needs (`fetcher`
needs `requests`, `gtfs-fetcher` and `api` need nothing beyond `redis`/
`fastapi`+`uvicorn`). Pick the extras you need, comma-separated, no spaces:

```
pip install -e ".[fetcher,gtfs-fetcher,api,dev]"
```

(`dev` adds `pytest`/`ruff`/`httpx2` for running the tests below; skip it in
production images.)

Run the GTI fetcher:
```
GTI_USER=... GTI_HMAC_SECRET=... hvvmap-fetcher
```
(or via `credentials.json`, see `hvv_map.gti_client`)

`SEGMENT_CACHE_PATH` defaults to a bare relative filename, resolved against
whatever directory a command happens to run in - if you invoke `hvvmap-*`
commands from outside a container (own venv, not `docker exec`), set it to
an absolute path once in your shell profile, or every run from a different
directory silently creates a fresh, empty database instead of using the
real one:
```
export SEGMENT_CACHE_PATH=/absolute/path/to/segment_cache.db
```

Run the GTFS fetcher (needs a static GTFS feed - `agency.txt`, `routes.txt`,
`trips.txt`, `stop_times.txt`, `stops.txt`, `calendar.txt`/
`calendar_dates.txt`, `shapes.txt` - unzipped into one directory):
```
GTFS_DIR=/path/to/gtfs hvvmap-gtfs-fetcher
```
`GTFS_DIR` defaults to `data/gtfs`, relative like `SEGMENT_CACHE_PATH` above
- same caveat about the current working directory applies.

Run the API:
```
REDIS_HOST=127.0.0.1 uvicorn hvv_map.api:app --reload
```

Run the tests:
```
pytest
```
Tests default to `REDIS_DB=15` (see `tests/conftest.py`) so they never
touch production data on a shared Redis instance.

### First run

`hvv:stations` (needed to resolve disruption coordinates) self-heals within
`REFERENCE_REBUILD_INTERVAL` (30 min by default) of a fresh start -
`finish_reference_rebuild()` persists it as a side effect of station data it
already fetches. If you don't want to wait, run once manually:

```
hvvmap-stations
```

Everything else self-starts too: `hvv:positions`/`hvv:positions_realtime`,
`hvv:announcements`/`hvv:disruptions`, and the reference layers
(`hvv:reference_lines`/`hvv:reference_stops`) all begin populating within
the fetcher's first few loop cycles - though the reference layers start out
empty (or nearly so) and fill in gradually as the fetcher observes real
vehicle movement, not instantly. The GTFS layers need no such warm-up -
they're built directly from the static feed on first run.

### One-off CLI commands

- `hvvmap-stations` - immediate `hvv:stations` refresh instead of waiting
  for the next automatic reference rebuild; safe to re-run any time
- `hvvmap-lines` - a lookup tool for line names/ids (`hvvmap-lines A1`
  filters by name); not read by the running system, purely diagnostic
- `hvvmap-reference` - (re)build `hvv:reference_lines`/`hvv:reference_stops`
  immediately, instead of waiting for the fetcher's own periodic rebuild
- `hvvmap-fetch-vehiclemap` / `hvvmap-fetch-announcements` - one-shot fetches
  for manual inspection via `redis-cli`

`scripts/` holds standalone diagnostics (line stability, gap analysis, bus
vehicle-type checks) - not part of the installed package.

## Frontend

`static/` ships four map pages, all served by the same `api.py`:

- `leaflet_map_hvv_live.html` / `_realtime.html` / `_gtfs.html` - three
  near-identical Leaflet pages, one per data source, sharing
  `leaflet_map_hvv_base.js`/`leaflet_map_hvv.css`; only their positions
  endpoint and PWA manifest differ
- `maplibre_map_hvv_live.html` - a separate, native MapLibre GL JS
  implementation (no Leaflet), switching between all three sources
  (`live`/`realtime`/`gtfs`) at runtime via a dropdown, no page reload
  needed; see `maplibre_map_hvv_base.js`'s `SOURCES`. Defaults to
  `realtime` - see "Notable data quirks" below for why.

`index.html` is the landing page linking to all of them.

## Docker

Three images, three Dockerfiles:

```
docker build -f Dockerfile.fetcher -t hvvmap-fetcher .
docker build -f Dockerfile.gtfs-fetcher -t hvvmap-gtfs-fetcher .
docker build -f Dockerfile.api -t hvvmap-api .
```

The **api** image expects, copied in at build time:
- `static/` - the frontend (HTML/JS/CSS, PWA manifests, icons)
- `fonts/` - vector-tile glyphs (only the font stacks the style actually
  uses, see the style's `text-font` entries)
- `positron_style.json` - the MapLibre style (`{name}_style.json` naming;
  referenced as `/api/vector/style/{name}.json`)

and, mounted as a volume at runtime:
- any single `*.mbtiles` file under `/osm/` - filename doesn't matter, the
  API picks up whatever's there

The **gtfs-fetcher** image needs a static GTFS feed mounted at `GTFS_DIR`
(default `data/gtfs`) - unzipped, not the raw `.zip`.

All images read `REDIS_HOST`/`REDIS_DB` (default `127.0.0.1`/`0`); only the
GTI fetcher needs `GTI_USER`/`GTI_HMAC_SECRET`.

## Notable data quirks

- S1 has a data quirk where some journey representations carry a stale
  `destination` - see `_s1_should_keep()` in `geojson.py`.
- `hvv:positions` is built from the `realtime=False` API variant,
  `hvv:positions_realtime` from `realtime=True` - both actively served
  (`/api/hvv/live/positions.geojson` and `/api/hvv/realtime/...`), not just
  kept for comparison. The `realtime=False` variant does not reflect
  unplanned service detours (e.g. a closure) and keeps showing journeys
  along the original route; `realtime=True` reflects at least some such
  detours, though inconsistently across lines (some correctly truncated,
  others still showing unaffected full-length trips) - neither is fully
  reliable, but `realtime=True` is the closer approximation, hence the
  MapLibre page's default.
- The fetcher makes an actual `getVehicleMap` call only every
  `VEHICLE_MAP_FETCH_INTERVAL` seconds; both `hvv:positions` and
  `hvv:positions_realtime` are re-interpolated every loop cycle from the
  last fetched data, keeping movement smooth without polling that often.
- `segment_cache.db` (SQLite) grows organically from observed vehicle
  movement; entries not re-observed within `STALE_THRESHOLD_SECONDS` age
  out of the reference layers automatically.
- GTFS-based positions interpolate linearly between two consecutive stops,
  not along the actual `shapes.txt` geometry - matching a trip's stops to
  its shape reliably would need `shape_dist_traveled`, which isn't
  guaranteed present/accurate in the feed. The reference **lines** layer
  doesn't have this problem: it draws each trip's full `shapes.txt`
  polyline directly, since `trips.txt` links a trip to its shape_id with no
  matching needed.
- GTFS mode assignment (U/S/AKN/ferry) is mostly a direct `route_type`
  lookup, but replacement buses run under `route_type=3` (regular bus) like
  any other bus line - `gtfs_schedule.py`'s `SPECIAL_BUS_ROUTES` maps their
  route_ids to the mode they replace.
- `calendar_dates.txt` exceptions (added/removed single-day services) are
  respected in `gtfs_schedule.py`.
