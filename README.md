# HVVMap

Live HVV transit data (U-Bahn, S-Bahn, AKN, ferry) for Hamburg, shown on a
web map.

## Architecture

Two independent deployables sharing one Python package (`hvv_map`) and one
Redis instance:

- **fetcher** (`hvv_map.fetcher`) - background loop polling the HVV GTI API
  (`getVehicleMap`, `getAnnouncements`, `listLines`, `listStations`),
  respecting its 1 req/s rate limit. Writes GeoJSON layers to Redis:
  `hvv:positions`, `hvv:disruptions`, `hvv:reference_lines`,
  `hvv:reference_stops`. Needs GTI credentials, no web-facing port.
- **api** (`hvv_map.api`) - FastAPI app serving those Redis-cached layers
  under `/api/hvv/live/*.geojson`, plus a background vector basemap (a
  single `.mbtiles` file) and the static frontend. Needs only Redis, no GTI
  credentials.

## Development

No `requirements.txt` - dependencies live in `pyproject.toml`, grouped into
optional extras so each deployable only pulls in what it needs (`fetcher`
needs `requests`, `api` needs `fastapi`/`uvicorn`, both need `redis`). Pick
the extras you need, comma-separated, no spaces:

```
pip install -e ".[fetcher,api,dev]"
```

(`dev` adds `pytest`/`ruff`/`httpx2` for running the tests below; skip it in
production images.)

Run the fetcher:
```
GTI_USER=... GTI_HMAC_SECRET=... hvvmap-fetcher
```
(or via `credentials.json`, see `hvv_map.gti_client`)

Run the API:
```
REDIS_HOST=127.0.0.1 uvicorn hvv_map.api:app --reload
```

Run the tests:
```
pytest
```

### First run

`hvvmap-fetcher` needs one manual one-time step before disruptions show up
correctly - it never does this on its own:

```
hvvmap-stations
```

Without it, `hvv:disruptions` stays empty forever (announcements have no
station coordinates to resolve against). Everything else self-starts:
`hvv:positions`, `hvv:announcements`/`hvv:disruptions`, and the reference
layers (`hvv:reference_lines`/`hvv:reference_stops`) all begin populating
within the fetcher's first few loop cycles - though the reference layers
start out empty (or nearly so) and fill in gradually as the fetcher
observes real vehicle movement, not instantly.

### One-off CLI commands

- `hvvmap-stations` - see "First run" above; safe to re-run any time
- `hvvmap-lines` - a lookup tool for line names/ids (`hvvmap-lines A1`
  filters by name); not read by the running system, purely diagnostic
- `hvvmap-reference` - (re)build `hvv:reference_lines`/`hvv:reference_stops`
  immediately, instead of waiting for the fetcher's own periodic rebuild
- `hvvmap-fetch-vehiclemap` / `hvvmap-fetch-announcements` - one-shot fetches
  for manual inspection via `redis-cli`

`scripts/` holds standalone diagnostics (line stability, gap analysis, bus
vehicle-type checks) - not part of the installed package.

## Docker

Two images, two Dockerfiles:

```
docker build -f Dockerfile.fetcher -t hvvmap-fetcher .
docker build -f Dockerfile.api -t hvvmap-api .
```

The **api** image expects, copied in at build time:
- `static/` - the frontend (HTML/JS/CSS, PWA manifest, icons)
- `fonts/` - vector-tile glyphs (only the font stacks the style actually
  uses, see the style's `text-font` entries)
- `positron_style.json` - the MapLibre style (`{name}_style.json` naming;
  referenced as `/api/vector/style/{name}.json`)

and, mounted as a volume at runtime:
- any single `*.mbtiles` file under `/osm/` - filename doesn't matter, the
  API picks up whatever's there

Both images read `REDIS_HOST` (default `127.0.0.1`); only the fetcher needs
`GTI_USER`/`GTI_HMAC_SECRET`.

## Notable data quirks

- S1 has a data quirk where some journey representations carry a stale
  `destination` - see `_s1_should_keep()` in `geojson.py`.
- `hvv:positions` is built from the `realtime=False` API variant only,
  for `journeyID` stability; `hvv:vehiclemap:realtime` is kept for
  comparison, but not otherwise used.
- The fetcher makes an actual `getVehicleMap` call only every
  `VEHICLE_MAP_FETCH_INTERVAL` seconds; `hvv:positions` itself is
  re-interpolated every loop cycle from the last fetched data, keeping
  movement smooth without polling that often.
- `segment_cache.db` (SQLite) grows organically from observed vehicle
  movement; entries not re-observed within `STALE_THRESHOLD_SECONDS` age
  out of the reference layers automatically.
