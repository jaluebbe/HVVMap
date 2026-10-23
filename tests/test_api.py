import gzip
import json
import sqlite3

import pytest
import redis
from fastapi.testclient import TestClient

import hvv_map.api as api_module
from hvv_map.course_log import log_calls
from hvv_map.redis_client import get_redis_client


@pytest.fixture
def client(tmp_path, monkeypatch):
    osm_dir = tmp_path / "osm"
    osm_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<html>index</html>")
    (tmp_path / "static" / "maplibre_map_hvv_gtilive.html").write_text(
        "<html>map</html>"
    )
    (tmp_path / "static" / "maplibre_map_hvv_ubahn.html").write_text(
        "<html>ubahn</html>"
    )
    (tmp_path / "fonts").mkdir()

    conn = sqlite3.connect(osm_dir / "test.mbtiles")
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.execute(
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
        "tile_row INTEGER, tile_data BLOB)"
    )
    conn.execute("INSERT INTO metadata VALUES ('minzoom', '0')")
    conn.execute("INSERT INTO metadata VALUES ('maxzoom', '14')")
    conn.execute("INSERT INTO metadata VALUES ('json', '{\"vector_layers\": []}')")
    conn.execute(
        "INSERT INTO tiles VALUES (5, 10, 20, ?)", (gzip.compress(b"fake-pbf"),)
    )
    conn.commit()
    conn.close()

    (tmp_path / "basic_style.json").write_text(
        json.dumps(
            {
                "version": 8,
                "sources": {"openmaptiles": {"type": "vector"}},
                "sprite": "https://example.com/sprite",
                "glyphs": "https://example.com/{fontstack}/{range}.pbf",
                "layers": [],
            }
        )
    )

    monkeypatch.setattr(api_module, "OSM_DIR", osm_dir)
    monkeypatch.setattr(api_module, "_db_connection", None)
    return TestClient(api_module.app)


def test_root_serves_the_gtilive_map(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "<html>map</html>" in response.text


def test_ubahn_shortcut_serves_the_ubahn_map(client):
    response = client.get("/ubahn", follow_redirects=False)
    assert response.status_code == 200
    assert "<html>ubahn</html>" in response.text


def test_robots_txt_excludes_api(client):
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /api/" in response.text


def test_vector_metadata_includes_dynamic_tiles_url(client):
    response = client.get("/api/vector/metadata.json")
    assert response.status_code == 200
    data = response.json()
    assert data["minzoom"] == 0
    assert data["maxzoom"] == 14
    assert data["tiles"] == ["http://testserver/api/vector/tiles/{z}/{x}/{y}.pbf"]


def test_vector_metadata_503_without_mounted_basemap(tmp_path, monkeypatch):
    empty_osm_dir = tmp_path / "empty_osm"
    empty_osm_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_module, "OSM_DIR", empty_osm_dir)
    monkeypatch.setattr(api_module, "_db_connection", None)
    client = TestClient(api_module.app)

    response = client.get("/api/vector/metadata.json")
    assert response.status_code == 503


def test_vector_tile_found_returns_gzip_content(client):
    # tile_row=20, zoom=5 -> y = 2**5 - 1 - 20 = 11
    response = client.get("/api/vector/tiles/5/10/11.pbf")
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.content == b"fake-pbf"


def test_vector_tile_not_found_returns_404(client):
    response = client.get("/api/vector/tiles/5/99/99.pbf")
    assert response.status_code == 404


def test_vector_style_rewrites_source_url_sprite_and_glyphs(client):
    response = client.get("/api/vector/style/basic.json")
    assert response.status_code == 200
    style = response.json()
    assert (
        style["sources"]["openmaptiles"]["url"]
        == "http://testserver/api/vector/metadata.json"
    )
    assert style["sprite"] == "http://testserver/static/sprites/basic"
    assert style["glyphs"] == "http://testserver/fonts/{fontstack}/{range}.pbf"


def test_vector_style_404_for_unknown_style(client):
    response = client.get("/api/vector/style/unknown.json")
    assert response.status_code == 404


def test_live_endpoint_503_when_redis_key_missing(client):
    redis_client = get_redis_client()
    redis_client.delete("hvv:positions_gtilive")
    response = client.get("/api/hvv/gtilive/positions.geojson")
    assert response.status_code == 503


@pytest.mark.parametrize(
    "path,redis_key",
    [
        ("/api/hvv/gtilive/positions.geojson", "hvv:positions_gtilive"),
        ("/api/hvv/live/disruptions.geojson", "hvv:disruptions"),
        ("/api/hvv/live/announcements.json", "hvv:announcements"),
    ],
)
def test_live_endpoint_returns_stored_geojson(client, path, redis_key):
    redis_client = get_redis_client()
    geojson = {"type": "FeatureCollection", "features": [{"marker": path}]}
    redis_client.set(redis_key, json.dumps({"fetched_at": 1, "data": geojson}))

    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == geojson


def test_announcement_categories_classifies_each_by_id(client):
    redis_client = get_redis_client()
    announcements = {
        "announcements": [
            {"id": "a1", "summary": "U1-Sperrung", "description": ""},
            {
                "id": "a2",
                "summary": "Aufzug kaputt",
                "description": "Rollstuhl / Kinderwagen",
            },
            {"id": "a3", "summary": "Fahrplanänderung", "description": ""},
        ]
    }
    redis_client.set(
        "hvv:announcements", json.dumps({"fetched_at": 1, "data": announcements})
    )

    response = client.get("/api/hvv/live/announcement-categories.json")
    assert response.status_code == 200
    assert response.json() == {
        "a1": "SPERRUNG",
        "a2": "BARRIEREFREIHEIT",
        "a3": "SONSTIGE",
    }


def test_announcement_categories_503_when_announcements_missing(client):
    redis_client = get_redis_client()
    redis_client.delete("hvv:announcements")
    response = client.get("/api/hvv/live/announcement-categories.json")
    assert response.status_code == 503


def test_redis_connection_error_does_not_crash_import():
    # get_redis_client() only opens a lazy connection object - importing
    # api.py must not require a live Redis server.
    assert isinstance(api_module._redis_client, redis.Redis)


class _FakeRedis:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        value = self.values.get(key)
        return json.dumps(value) if value is not None else None


_ANCHOR = {"fetched_at": 1, "data": {"date": "2026-10-16", "time": "04:00:00"}}


def _departure(offset):
    return {
        "line": {"id": "L", "name": "U1", "type": {"shortInfo": "U"}},
        "station": {"id": "s1", "combinedName": "Ohlstedt"},
        "timeOffset": offset,
        "serviceId": "1",
    }


def test_departures_by_station_returns_tree_with_meta(client, monkeypatch):
    fake = _FakeRedis(
        {
            "hvv:departures:merged": {
                "fetched_at": 42,
                "data": {"departures": [_departure(5)]},
            },
            "hvv:departures:anchor_start": _ANCHOR,
        }
    )
    monkeypatch.setattr(api_module, "_redis_client", fake)

    data = client.get("/api/hvv/live/departures-by-station.json").json()

    assert data["fetched_at"] == 42
    assert data["anchor"] == "2026-10-16 04:00"
    line = data["modes"][0]["lines"][0]
    assert line["stations"][0]["departures"][0]["time"] == "04:05"


def test_departures_endpoints_work_without_anchor(client, monkeypatch):
    fake = _FakeRedis(
        {
            "hvv:departures:merged": {
                "fetched_at": 1,
                "data": {"departures": [_departure(5)]},
            }
        }
    )
    monkeypatch.setattr(api_module, "_redis_client", fake)

    data = client.get("/api/hvv/live/departures-by-station.json").json()

    assert data["anchor"] is None


def test_departures_endpoints_503_when_cache_missing(client, monkeypatch):
    monkeypatch.setattr(api_module, "_redis_client", _FakeRedis({}))
    assert client.get("/api/hvv/live/departures-by-station.json").status_code == 503


def test_gtilive_debug_returns_lines_stops_and_summary(client, monkeypatch):
    stations = [
        {
            "id": sid,
            "name": sid,
            "city": "",
            "lon": lon,
            "lat": 53.5,
            "combined_name": sid,
        }
        for sid, lon in (("A", 10.0), ("B", 10.1))
    ]
    service = {
        "line": {"id": "L", "name": "U1", "type": {"shortInfo": "U"}},
        "stops": [],
        "subline": [{"id": "A", "name": "A"}, {"id": "B", "name": "B"}],
    }
    fake = _FakeRedis(
        {
            "hvv:departures:by_service": {"fetched_at": 1, "data": {"1": service}},
            "hvv:stations": {"fetched_at": 1, "data": stations},
        }
    )
    monkeypatch.setattr(api_module, "_redis_client", fake)
    monkeypatch.setattr(api_module, "_get_gti_shapes_connection", lambda: None)

    data = client.get("/api/hvv/gtilive/debug.geojson").json()

    kinds = sorted(f["properties"]["kind"] for f in data["features"])
    assert kinds == ["line", "stop", "stop"]
    assert data["debug"]["resolved"] == 1


def test_gtilive_debug_503_when_by_service_missing(client, monkeypatch):
    monkeypatch.setattr(api_module, "_redis_client", _FakeRedis({}))
    assert client.get("/api/hvv/gtilive/debug.geojson").status_code == 503


def test_gtilive_courses_empty_without_database(client, monkeypatch):
    monkeypatch.setattr(api_module, "_get_gti_shapes_connection", lambda: None)
    data = client.get("/api/hvv/gtilive/courses.json").json()
    assert data == {"calls": [], "runs": []}


def test_departures_live_returns_tree_with_delay_and_live_anchor(client, monkeypatch):
    dep = {**_departure(5), "delay": 120}
    fake = _FakeRedis(
        {
            "hvv:departures:live": {
                "fetched_at": 7,
                "data": {
                    "departures": [dep],
                    "anchor": {"date": "2026-10-17", "time": "00:20:00"},
                },
            }
        }
    )
    monkeypatch.setattr(api_module, "_redis_client", fake)

    data = client.get("/api/hvv/live/departures-live.json").json()

    assert data["anchor"] == "2026-10-17 00:20"
    departure = data["modes"][0]["lines"][0]["stations"][0]["departures"][0]
    assert departure["time"] == "00:25" and departure["delay"] == 120


def test_departures_live_503_when_cache_missing(client, monkeypatch):
    monkeypatch.setattr(api_module, "_redis_client", _FakeRedis({}))
    assert client.get("/api/hvv/live/departures-live.json").status_code == 503


def test_trips_endpoint_matches_plan_with_live(client, monkeypatch):
    plan_stop = {
        "timeOffset": 302,
        "direction": "Z",
        "station": {"id": "A", "name": "A"},
        "platform": "Gleis 1",
    }
    line = {"name": "U1", "type": {"shortInfo": "U"}}
    fake = _FakeRedis(
        {
            "hvv:departures:by_service": {
                "fetched_at": 1,
                "data": {
                    "5": {
                        "line": line,
                        "directionId": 6,
                        "destination": "Z",
                        "stops": [plan_stop],
                    }
                },
            },
            "hvv:departures:anchor_start": {
                "fetched_at": 1,
                "data": {"date": "2026-10-17", "time": "04:00:00"},
            },
            "hvv:departures:live": {
                "fetched_at": 9,
                "data": {
                    "anchor": {"date": "2026-10-17", "time": "09:00:00"},
                    "departures": [
                        {
                            "line": {**line, "direction": "Z"},
                            "station": {"id": "A", "name": "A"},
                            "timeOffset": 2,
                            "directionId": 6,
                            "serviceId": 77,
                            "delay": 60,
                        }
                    ],
                },
            },
        }
    )
    monkeypatch.setattr(api_module, "_redis_client", fake)

    data = client.get("/api/hvv/live/trips.json").json()

    assert data["fetched_at"] == 9
    assert data["summary"]["live_matched"] == 1
    assert data["trips"][0]["live_ids"] == ["77"]


def test_trips_endpoint_503_when_cache_missing(client, monkeypatch):
    monkeypatch.setattr(api_module, "_redis_client", _FakeRedis({}))
    assert client.get("/api/hvv/live/trips.json").status_code == 503


@pytest.mark.parametrize(
    "path,redis_key",
    [
        ("/api/hvv/gtfs/lines.geojson", "hvv:gtfs:reference_lines"),
        ("/api/hvv/gtfs/stops.geojson", "hvv:gtfs:reference_stops"),
    ],
)
def test_gtfs_reference_layers_are_served_from_cache(client, path, redis_key):
    redis_client = get_redis_client()
    geojson = {"type": "FeatureCollection", "features": [{"marker": path}]}
    redis_client.set(redis_key, json.dumps({"fetched_at": 1, "data": geojson}))
    api_module._gtfs_lines_cache._built_at = float("-inf")
    api_module._gtfs_stops_cache._built_at = float("-inf")

    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == geojson
    assert (
        client.get(
            path, headers={"If-None-Match": response.headers["etag"]}
        ).status_code
        == 304
    )


def test_static_cache_headers(client, tmp_path):
    (tmp_path / "static" / "maplibre-gl").mkdir()
    (tmp_path / "static" / "maplibre-gl" / "lib.js").write_text("x")
    (tmp_path / "fonts" / "f.pbf").write_bytes(b"x")
    assert client.get("/static/index.html").headers["cache-control"] == "no-cache"
    assert (
        "max-age=604800"
        in client.get("/static/maplibre-gl/lib.js").headers["cache-control"]
    )
    assert "max-age=604800" in client.get("/fonts/f.pbf").headers["cache-control"]


def test_known_courses_returns_the_newest_course_per_live_service_id(
    client, monkeypatch
):
    stops = [{"name": "A", "dep": "2026-10-21T19:56", "arr": None}]
    fake = _FakeRedis(
        {
            "hvv:trips:courses": {
                "fetched_at": 1,
                "data": {
                    "old": {"fetched_at": 10, "service_id": "7", "stops": stops},
                    "new": {
                        "fetched_at": 20,
                        "service_id": "7",
                        "stops": stops,
                        "vehicles": [{"number": "417"}, {"id": "9"}],
                    },
                    "failed": {"fetched_at": 30, "service_id": "8", "stops": None},
                    "other": {"fetched_at": 30, "service_id": "9", "stops": stops},
                },
            }
        }
    )
    monkeypatch.setattr(api_module, "_redis_client", fake)

    data = client.get("/api/hvv/live/courses.json?service_ids=7,8").json()

    assert list(data["courses"]) == ["7"]
    assert data["courses"]["7"]["fetched_at"] == 20
    assert data["courses"]["7"]["vehicles"] == ["417", "9"]


def test_course_log_returns_the_newest_calls_first(client):
    redis_client = get_redis_client()
    redis_client.delete("hvv:courses:log")
    log_calls(
        redis_client,
        [{"kind": "track", "at": 1}, {"kind": "trip", "at": 2}],
    )

    calls = client.get("/api/hvv/live/course-log.json").json()["calls"]

    assert [c["at"] for c in calls] == [2, 1]
    redis_client.delete("hvv:courses:log")


def test_course_cache_counts_live_ids_by_course_state(client, monkeypatch):
    line = {"name": "U1", "type": {"shortInfo": "U"}}
    deps = [
        {"line": line, "serviceId": sid, "timeOffset": 1, "station": {}}
        for sid in (1, 2, 3)
    ]
    stops = [{"id": "A", "dep": "2026-10-22T09:00"}]
    now = api_module.time.time()
    fake = _FakeRedis(
        {
            "hvv:trips:courses": {
                "fetched_at": 1,
                "data": {
                    "live:1": {"fetched_at": now - 60, "stops": stops},
                    "live:2": {"fetched_at": now - 600, "stops": None},
                },
            },
            "hvv:departures:live": {"fetched_at": 5, "data": {"departures": deps}},
        }
    )
    monkeypatch.setattr(api_module, "_redis_client", fake)

    data = client.get("/api/hvv/live/course-cache.json").json()

    assert data["live_fetched_at"] == 5
    [row] = data["lines"]
    assert (row["line"], row["live"], row["ok"], row["failed"], row["missing"]) == (
        "U1",
        3,
        1,
        1,
        1,
    )
    assert data["total"]["stale"] == 0 and 55 <= data["total"]["age"]["median"] <= 65
