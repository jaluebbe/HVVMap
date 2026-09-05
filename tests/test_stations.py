from unittest.mock import MagicMock

from hvv_map.stations import (
    StationInfo,
    by_id,
    fetch_stations,
    load_stations,
    store_stations,
)
from hvv_map.redis_client import get_redis_client

FAKE_RESPONSE = {
    "stations": [
        {
            "id": "Master:52982",
            "name": "Neumühlen/Övelgönne",
            "city": "Hamburg",
            "coordinate": {"x": 9.9, "y": 53.5},
        },
        {"id": "Master:19072", "exists": False},  # deleted entry, no coordinate
    ]
}


def _client_with(response):
    client = MagicMock()
    client.send.return_value = response
    return client


def test_fetch_stations_parses_fields():
    stations = fetch_stations(_client_with(FAKE_RESPONSE))
    assert stations == [
        StationInfo(
            id="Master:52982",
            name="Neumühlen/Övelgönne",
            city="Hamburg",
            lon=9.9,
            lat=53.5,
        )
    ]


def test_fetch_stations_skips_entries_without_coordinate():
    stations = fetch_stations(_client_with(FAKE_RESPONSE))
    assert len(stations) == 1  # the deleted entry is dropped


def test_fetch_stations_sends_empty_data_release_id():
    client = _client_with(FAKE_RESPONSE)
    fetch_stations(client)
    request = client.send.call_args[0][1]
    assert request["dataReleaseID"] == ""


def test_by_id():
    stations = fetch_stations(_client_with(FAKE_RESPONSE))
    index = by_id(stations)
    assert index["Master:52982"].name == "Neumühlen/Övelgönne"


def test_store_and_load_roundtrip():
    redis_client = get_redis_client()
    stations = [StationInfo(id="a", name="Test", city="Hamburg", lon=1.0, lat=2.0)]
    store_stations(redis_client, stations)
    assert load_stations(redis_client) == stations


def test_load_returns_empty_list_when_nothing_stored():
    redis_client = get_redis_client()
    redis_client.delete("hvv:stations")
    assert load_stations(redis_client) == []
