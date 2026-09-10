import gzip
import json
import sqlite3

import pytest
import redis
from fastapi.testclient import TestClient

import hvv_map.api as api_module
from hvv_map.redis_client import get_redis_client


@pytest.fixture
def client(tmp_path, monkeypatch):
    osm_dir = tmp_path / "osm"
    osm_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<html>index</html>")
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


def test_root_redirects_to_index(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/static/index.html"


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
    redis_client.delete("hvv:positions")
    response = client.get("/api/hvv/live/positions.geojson")
    assert response.status_code == 503


@pytest.mark.parametrize(
    "path,redis_key",
    [
        ("/api/hvv/live/positions.geojson", "hvv:positions"),
        ("/api/hvv/realtime/positions.geojson", "hvv:positions_realtime"),
        ("/api/hvv/live/disruptions.geojson", "hvv:disruptions"),
        ("/api/hvv/live/announcements.json", "hvv:announcements"),
        ("/api/hvv/live/stops.geojson", "hvv:reference_stops"),
        ("/api/hvv/live/lines.geojson", "hvv:reference_lines"),
    ],
)
def test_live_endpoint_returns_stored_geojson(client, path, redis_key):
    redis_client = get_redis_client()
    geojson = {"type": "FeatureCollection", "features": [{"marker": path}]}
    redis_client.set(redis_key, json.dumps({"fetched_at": 1, "data": geojson}))

    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == geojson


def test_redis_connection_error_does_not_crash_import():
    # get_redis_client() only opens a lazy connection object - importing
    # api.py must not require a live Redis server.
    assert isinstance(api_module._redis_client, redis.Redis)
