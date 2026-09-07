import json
from unittest.mock import patch

import hvv_map.gtfs_fetcher as gtfs_fetcher
from hvv_map.redis_client import get_redis_client


def test_store_writes_envelope_with_fetched_at():
    redis_client = get_redis_client()
    gtfs_fetcher._store(redis_client, "hvv:gtfs:test-key", {"a": 1}, ttl=10)

    stored = json.loads(redis_client.get("hvv:gtfs:test-key"))
    assert stored["data"] == {"a": 1}
    assert isinstance(stored["fetched_at"], int)


def test_rebuild_reference_stores_both_layers():
    redis_client = get_redis_client()
    redis_client.delete(gtfs_fetcher.LINES_KEY, gtfs_fetcher.STOPS_KEY)
    fake_schedule = object()  # opaque - build_* below are mocked, so its shape doesn't matter

    with patch(
        "hvv_map.gtfs_fetcher.build_lines_geojson",
        return_value={"type": "FeatureCollection", "features": ["lines"]},
    ), patch(
        "hvv_map.gtfs_fetcher.build_stops_geojson",
        return_value={"type": "FeatureCollection", "features": ["stops"]},
    ):
        gtfs_fetcher._rebuild_reference(redis_client, fake_schedule)

    lines = json.loads(redis_client.get(gtfs_fetcher.LINES_KEY))
    stops = json.loads(redis_client.get(gtfs_fetcher.STOPS_KEY))
    assert lines["data"]["features"] == ["lines"]
    assert stops["data"]["features"] == ["stops"]


def test_fetch_once_stores_all_three_layers(tmp_path, monkeypatch):
    monkeypatch.setattr(gtfs_fetcher, "GTFS_DIR", str(tmp_path))
    redis_client = get_redis_client()
    for key in (gtfs_fetcher.POSITIONS_KEY, gtfs_fetcher.LINES_KEY, gtfs_fetcher.STOPS_KEY):
        redis_client.delete(key)

    with patch("hvv_map.gtfs_fetcher.load_schedule", return_value=object()), patch(
        "hvv_map.gtfs_fetcher.build_positions_geojson",
        return_value={"type": "FeatureCollection", "features": []},
    ), patch(
        "hvv_map.gtfs_fetcher.build_lines_geojson",
        return_value={"type": "FeatureCollection", "features": []},
    ), patch(
        "hvv_map.gtfs_fetcher.build_stops_geojson",
        return_value={"type": "FeatureCollection", "features": []},
    ):
        gtfs_fetcher.fetch_once()

    for key in (gtfs_fetcher.POSITIONS_KEY, gtfs_fetcher.LINES_KEY, gtfs_fetcher.STOPS_KEY):
        assert redis_client.get(key) is not None


def test_routes_txt_mtime_reads_file_modification_time(tmp_path, monkeypatch):
    monkeypatch.setattr(gtfs_fetcher, "GTFS_DIR", str(tmp_path))
    (tmp_path / "routes.txt").write_text("route_id\n")

    mtime = gtfs_fetcher._routes_txt_mtime()

    assert mtime > 0
