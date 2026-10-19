# HVVMap

Live and schedule-based HVV transit data (U-Bahn, S-Bahn, AKN, ferry) for
Hamburg, shown on a web map.

## Architecture

Three independent deployables sharing one Python package (`hvv_map`) and
one Redis instance:

- **fetcher** (`hvv_map.fetcher`) - background loop polling the HVV GTI API
  (`getAnnouncements`, `listLines`, `listStations`, `departureList`,
  `departureCourse`), respecting its 1 req/s rate limit. Writes to Redis:
  `hvv:disruptions`, `hvv:departures:*`, `hvv:trips:courses`, `hvv:stations`,
  `hvv:sublines`. A second thread (`hvv_map.trip_positions`) computes the
  vehicle positions once per second from that data and writes
  `hvv:positions_gtilive`. Needs GTI credentials, no web-facing port.
- **gtfs-fetcher** (`hvv_map.gtfs_fetcher`) - background loop building the
  same kind of layers purely from a static GTFS feed on disk - no GTI
  credentials, no live API, no disruptions (a static feed has none). Writes
  `hvv:gtfs:positions`, `hvv:gtfs:reference_lines`, `hvv:gtfs:reference_stops`.
- **api** (`hvv_map.api`) - FastAPI app serving all of the above under
  `/api/hvv/live/*`, `/api/hvv/gtilive/*` and `/api/hvv/gtfs/*`, plus a
  background vector basemap (a single `.mbtiles` file) and the static
  frontend. Needs only Redis, no GTI credentials, no GTFS feed.

Supporting modules (all under `src/hvv_map/`):

| Module | Purpose |
|---|---|
| `gti_client.py` | GTI API HTTP client (signing, credentials) |
| `redis_client.py` | shared Redis connection helper |
| `lines.py` / `stations.py` | GTI line/station reference data |
| `departures.py` / `departures_live.py` | planned (`hvv:departures:by_service`) and live (`hvv:departures:live`) departure snapshots |
| `trip_courses.py` | `departureCourse` per running trip (`hvv:trips:courses`) |
| `trips.py` | trip table: planned trips compared with live departures and courses |
| `trip_position.py` | locates a trip at a stop or between two stops at a given time |
| `trip_positions.py` | builds `hvv:positions_gtilive` from the trip table, once per second |
| `gti_shapes.py` / `gtilive_geojson.py` | track geometry per subline (SQLite) and the `gtilive` lines/stops layers |
| `geometry.py` | distance helpers for walking along a polyline |
| `announcement_categories.py` | our own SPERRUNG/BARRIEREFREIHEIT/SONSTIGE classification per announcement - dependency-free (stdlib only), importable by both the fetcher and the API server |
| `disruptions.py` | builds `hvv:disruptions` from a `getAnnouncements` response, using `announcement_categories.py` for classification |
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

`GTI_SHAPES_DB_PATH` defaults to a bare relative filename, resolved against
whatever directory a command happens to run in - if you invoke `hvvmap-*`
commands from outside a container (own venv, not `docker exec`), set it to
an absolute path once in your shell profile, or every run from a different
directory silently creates a fresh, empty database instead of using the
real one:
```
export GTI_SHAPES_DB_PATH=/absolute/path/to/gti_shapes.db
```

Run the GTFS fetcher (needs a static GTFS feed - `agency.txt`, `routes.txt`,
`trips.txt`, `stop_times.txt`, `stops.txt`, `calendar.txt`/
`calendar_dates.txt`, `shapes.txt` - unzipped into one directory):
```
GTFS_DIR=/path/to/gtfs hvvmap-gtfs-fetcher
```
`GTFS_DIR` defaults to `data/gtfs`, relative like `GTI_SHAPES_DB_PATH` above
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

Everything else self-starts too: `hvv:announcements`/`hvv:disruptions`,
the departure snapshots and `hvv:positions_gtilive` begin populating within
the fetcher's first few loop cycles; the positions fill in as the trip
courses and subline tracks arrive. The GTFS layers need no such warm-up -
they're built directly from the static feed on first run.

### One-off CLI commands

- `hvvmap-stations` - immediate `hvv:stations` refresh instead of waiting
  for the fetcher's next automatic one (every 6 h); safe to re-run any time
- `hvvmap-lines` - a lookup tool for line names/ids (`hvvmap-lines A1`
  filters by name); not read by the running system, purely diagnostic
- `hvvmap-fetch-announcements` - one-shot fetch for manual inspection via
  `redis-cli`
- `hvvmap-gtfs-fetch-once` - one-shot GTFS layer build, same idea for the
  `hvv:gtfs:*` keys

## Frontend

`static/` ships map pages built on MapLibre GL JS, all served by the same
`api.py`:

- `maplibre_map_hvv_live.html` - all modes; the data source (`gtilive`,
  `gtfs`) is switched at runtime via a dropdown, no page reload needed; see
  `maplibre_map_hvv_base.js`'s `SOURCES`. Defaults to `gtilive`.
- `maplibre_map_hvv_ubahn.html` - the same page restricted to the U-Bahn,
  centered on Betriebshof Barmbek, with U1-U4 toggleable individually. Page
  options are set via `window.HVV_PAGE_CONFIG` (see `maplibre_map_hvv_base.js`).
- `maplibre_map_hvv_gtilive.html` - only the `gtilive` source, no dropdown;
  this is the public map, also served at `/`.

The Leaflet pages were removed; the last state with them is tagged
`legacy-leaflet-vehiclemap` (`git checkout legacy-leaflet-vehiclemap`, or
`git show legacy-leaflet-vehiclemap:static/leaflet_map_hvv_live.html`).

`index.html` (under `/static/`) is an overview for developers linking to the
maps, the debug pages below and the API docs; it is not the start page.

`dev_announcements.html` is a developer-only page, only linked from
`index.html`: it fetches `/api/hvv/live/announcements.json` and lists every
raw announcement with its full fields (locations, validities, links, etc.),
collapsible per entry (state kept in `localStorage`) with text search and
filters by `reason`/`planned`/`broadcastRelevant` - for spotting patterns
while refining `disruptions.py`'s filtering logic. It also fetches
`/api/hvv/live/announcement-categories.json` and shows our own
SPERRUNG/BARRIEREFREIHEIT/SONSTIGE classification per announcement,
filterable the same way - rendered as a visually distinct, filled badge
labeled "eigene Kategorie" so it's never mistaken for an HVV API field.

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

The **fetcher** image reads the same feed (read-only mount at `GTFS_DIR`) if it
is there: rail hops GTI delivers no track for are then cut from the GTFS
shapes (`gtfs_tracks.py`). Without a feed this step is skipped.

All images read `REDIS_HOST`/`REDIS_DB` (default `127.0.0.1`/`0`); only the
GTI fetcher needs `GTI_USER`/`GTI_HMAC_SECRET`.

## History

The `getVehicleMap` implementation (fetcher job, `segment_cache`,
`reference_geojson`, the `live`/`realtime` sources and their endpoints) was
removed to save API requests. The last state with it is tagged
`legacy-vehiclemap` (`git show legacy-vehiclemap:src/hvv_map/fetcher.py`).

## Notable data quirks

- GTI delivers vehicle numbers only for the U-Bahn and only with the matched
  live departure; the trip table remembers them per trip (`hvv:trips:vehicles`)
  while a running trip has no live match.
- The GTI-Live positions are minute-accurate at best (planned and live times
  have no seconds), so vehicles departing in the same minute move in sync.
- Straight links (shapes with sparse points, e.g. replacement buses) are
  flagged `straight` and only drawn with the "Luftlinien" checkbox.
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
