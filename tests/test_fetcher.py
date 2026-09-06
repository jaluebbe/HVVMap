import json
import time
from unittest.mock import patch

import hvv_map.fetcher as fetcher
from hvv_map.fetcher import _is_wanted, _store
from hvv_map.lines import REPLACEMENT_BUS_MODES
from hvv_map.redis_client import get_redis_client


def test_non_regionalbus_always_wanted():
    for vehicle_type in ["U_BAHN", "S_BAHN", "A_BAHN", "SCHIFF"]:
        journey = {"vehicleType": vehicle_type, "line": {"id": "anything"}}
        assert _is_wanted(journey)


def test_known_replacement_bus_wanted():
    journey = {
        "vehicleType": "REGIONALBUS",
        "line": {"id": "HHA-B:U1-ERSATZ_HHA-B"},
    }
    assert _is_wanted(journey)


def test_unrelated_regionalbus_rejected():
    journey = {"vehicleType": "REGIONALBUS", "line": {"id": "HHA-B:175_HHA-B"}}
    assert not _is_wanted(journey)


def test_all_known_replacement_bus_ids_are_wanted():
    for line_id in REPLACEMENT_BUS_MODES:
        journey = {"vehicleType": "REGIONALBUS", "line": {"id": line_id}}
        assert _is_wanted(journey)


def test_store_with_publish_sends_same_payload_to_matching_channel():
    redis_client = get_redis_client()
    pubsub = redis_client.pubsub()
    pubsub.subscribe("hvv:test-channel")
    pubsub.get_message(timeout=1)  # discard the subscribe-confirmation event

    _store("hvv:test-channel", {"features": []}, ttl=10, publish=True)

    message = pubsub.get_message(timeout=2)
    assert message is not None
    assert message["channel"] == "hvv:test-channel"
    payload = json.loads(message["data"])
    assert payload["data"] == {"features": []}
    assert "fetched_at" in payload

    # published payload must match what was actually stored under the key
    stored = json.loads(redis_client.get("hvv:test-channel"))
    assert stored == payload


def test_store_without_publish_sends_no_message():
    redis_client = get_redis_client()
    pubsub = redis_client.pubsub()
    pubsub.subscribe("hvv:test-channel-2")
    pubsub.get_message(timeout=1)

    _store("hvv:test-channel-2", {"features": []}, ttl=10, publish=False)

    assert pubsub.get_message(timeout=1) is None


def _fake_response(now, journey_id="test-journey"):
    return {
        "returnCode": "OK",
        "journeys": [
            {
                "journeyID": journey_id,
                "vehicleType": "U_BAHN",
                "line": {"id": "HHA-U:U1_HHA-U", "name": "U1", "type": {}},
                "segments": [
                    {
                        "startStopPointKey": "A",
                        "endStopPointKey": "B",
                        "startDateTime": now,
                        "endDateTime": now + 20,
                        "destination": "Ohlstedt",
                        "track": {"track": [10.0, 53.5, 10.01, 53.51]},
                    }
                ],
            }
        ],
    }


def test_rebuild_positions_does_nothing_without_prior_fetch():
    fetcher._last_no_realtime_response = None
    redis_client = get_redis_client()
    redis_client.delete("hvv:positions")

    fetcher._rebuild_positions()

    assert redis_client.get("hvv:positions") is None


def test_fetch_vehicle_map_no_realtime_populates_positions():
    now = int(time.time())
    with patch.object(fetcher.gti, "send", return_value=_fake_response(now)):
        fetcher.fetch_vehicle_map(realtime=False)

    stored = json.loads(fetcher.redis_client.get("hvv:positions"))
    assert len(stored["data"]["features"]) == 1


def test_fetch_vehicle_map_realtime_does_not_touch_cached_response():
    fetcher._last_no_realtime_response = None
    now = int(time.time())
    with patch.object(fetcher.gti, "send", return_value=_fake_response(now)):
        fetcher.fetch_vehicle_map(realtime=True)

    assert fetcher._last_no_realtime_response is None


def test_rebuild_positions_reinterpolates_without_new_fetch():
    now = int(time.time())
    with patch.object(fetcher.gti, "send", return_value=_fake_response(now)):
        fetcher.fetch_vehicle_map(realtime=False)

    first = json.loads(fetcher.redis_client.get("hvv:positions"))
    first_progress = first["data"]["features"][0]["properties"]["progress"]

    # no new fetch - only a later time for re-interpolation
    with patch.object(time, "time", return_value=now + 10):
        fetcher._rebuild_positions()

    second = json.loads(fetcher.redis_client.get("hvv:positions"))
    second_progress = second["data"]["features"][0]["properties"]["progress"]
    assert second_progress > first_progress


def test_rebuild_positions_realtime_does_nothing_without_prior_fetch():
    fetcher._last_realtime_response = None
    redis_client = get_redis_client()
    redis_client.delete("hvv:positions_realtime")

    fetcher._rebuild_positions_realtime()

    assert redis_client.get("hvv:positions_realtime") is None


def test_fetch_vehicle_map_realtime_populates_positions_realtime():
    now = int(time.time())
    with patch.object(fetcher.gti, "send", return_value=_fake_response(now)):
        fetcher.fetch_vehicle_map(realtime=True)

    stored = json.loads(fetcher.redis_client.get("hvv:positions_realtime"))
    assert len(stored["data"]["features"]) == 1


def test_fetch_vehicle_map_no_realtime_does_not_touch_realtime_cache():
    fetcher._last_realtime_response = None
    now = int(time.time())
    with patch.object(fetcher.gti, "send", return_value=_fake_response(now)):
        fetcher.fetch_vehicle_map(realtime=False)

    assert fetcher._last_realtime_response is None


def test_rebuild_positions_realtime_reinterpolates_without_new_fetch():
    now = int(time.time())
    with patch.object(fetcher.gti, "send", return_value=_fake_response(now)):
        fetcher.fetch_vehicle_map(realtime=True)

    first = json.loads(fetcher.redis_client.get("hvv:positions_realtime"))
    first_progress = first["data"]["features"][0]["properties"]["progress"]

    with patch.object(time, "time", return_value=now + 10):
        fetcher._rebuild_positions_realtime()

    second = json.loads(fetcher.redis_client.get("hvv:positions_realtime"))
    second_progress = second["data"]["features"][0]["properties"]["progress"]
    assert second_progress > first_progress
