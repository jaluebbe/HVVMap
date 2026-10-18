from unittest.mock import patch

import hvv_map.fetcher as fetcher


class _FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, ex=None):
        self.values[key] = value


def test_fetch_stations_catalog_stores_the_station_list():
    stations = ["station"]
    fake_redis = _FakeRedis()
    with (
        patch.object(fetcher, "redis_client", fake_redis),
        patch.object(fetcher, "fetch_stations", return_value=stations) as fetch,
        patch.object(fetcher, "store_stations") as store,
    ):
        fetcher.fetch_stations_catalog()

    fetch.assert_called_once_with(fetcher.gti)
    store.assert_called_once_with(fake_redis, stations)


def test_store_wraps_the_data_with_a_timestamp_and_ttl():
    fake_redis = _FakeRedis()
    with patch.object(fetcher, "redis_client", fake_redis):
        fetcher._store("hvv:test", {"features": []}, ttl=10)

    import json

    payload = json.loads(fake_redis.values["hvv:test"])
    assert payload["data"] == {"features": []}
    assert "fetched_at" in payload


def test_fetch_gti_shapes_runs_the_update_and_closes_the_database():
    class _Conn:
        closed = False

        def close(self):
            self.closed = True

    conn = _Conn()
    summary = {"resolved": 2, "skipped": 0, "failed": 1, "no_query": 0}
    with (
        patch.object(fetcher, "open_shapes_db", return_value=conn),
        patch.object(
            fetcher, "run_gti_shapes_update", return_value=(summary, None)
        ) as update,
    ):
        fetcher.fetch_gti_shapes()

    update.assert_called_once()
    assert conn.closed
