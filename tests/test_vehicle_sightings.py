import json

import pytest

from hvv_map.redis_client import get_redis_client
from hvv_map.vehicle_sightings import (
    LAST_KEY,
    STREAM_KEY,
    course_sightings,
    extract_sightings,
    record_sightings,
)

U3 = {"name": "U3", "direction": "Barmbek", "type": {"shortInfo": "U"}}
S1 = {"name": "S1", "direction": "Wedel", "type": {"shortInfo": "S"}}


def _departure(line, offset, vehicles, service_id=7, delay=0):
    return {
        "line": line,
        "station": {"id": "Master:1", "combinedName": "Berliner Tor"},
        "timeOffset": offset,
        "serviceId": service_id,
        "delay": delay,
        "platform": "Gleis 2",
        "vehicles": vehicles,
    }


def _live(*departures):
    return {
        "anchor": {"date": "2026-10-20", "time": "16:00:00"},
        "departures": list(departures),
    }


COUPLED = [{"id": "a", "number": 432}, {"id": "b", "number": 301}]


@pytest.fixture
def redis_client():
    client = get_redis_client()
    client.flushdb()
    yield client
    client.flushdb()


def test_extract_gives_one_sighting_per_vehicle_with_its_coupled_partners():
    sightings = extract_sightings(
        _live(_departure(U3, 3, COUPLED), _departure(S1, 4, [{"number": 1234}]))
    )

    first, second = sightings[:2]
    assert (first["vehicle"], first["series"], first["with"]) == (
        "432",
        "DT5.2",
        ["301"],
    )
    assert (second["vehicle"], second["series"], second["with"]) == (
        "301",
        "DT5.1",
        ["432"],
    )
    assert first["planned"] == "2026-10-20T16:03" and first["line"] == "U3"
    assert sightings[2]["series"] is None  # no series derived outside the U-Bahn


def test_extract_skips_departures_without_vehicles_and_uses_the_id_as_fallback():
    sightings = extract_sightings(
        _live(_departure(U3, 3, None), _departure(U3, 5, [{"id": "x9"}]))
    )

    assert [s["vehicle"] for s in sightings] == ["id:x9"]


def test_record_writes_changes_only_and_keeps_the_latest_sightings(redis_client):
    live = _live(_departure(U3, 3, COUPLED))

    assert record_sightings(redis_client, live, now=1000) == 2
    assert record_sightings(redis_client, live, now=1240) == 0  # unchanged
    late = _live(_departure(U3, 3, COUPLED, delay=60))
    assert record_sightings(redis_client, late, now=1480) == 2

    assert redis_client.xlen(STREAM_KEY) == 4
    last = json.loads(redis_client.hget(LAST_KEY, "432"))
    assert last["seen_at"] == 1480 and last["sightings"][0]["delay"] == 60
    newest = json.loads(redis_client.xrange(STREAM_KEY)[-1][1]["data"])
    assert newest["seen_at"] == 1480


def test_course_sightings_take_the_vehicles_of_the_whole_course():
    request = {
        "station": {"name": "Wandsbek-Gartenstadt"},
        "time": "2026-10-20T15:46:00.000+0200",
        "lineId": "de:hvv:U3:",
        "serviceId": 24598,
    }
    response = {
        "vehicles": [
            {"id": "x_218", "number": "218"},
            {"id": "x_215", "number": "215"},
        ],
        "courseElements": [{"toStation": {"name": "Barmbek"}}],
    }

    first, second = course_sightings(request, response)

    assert (first["vehicle"], first["with"], first["series"]) == (
        "218",
        ["215"],
        "DT4.6",
    )
    assert first["source"] == "departureCourse" and first["destination"] == "Barmbek"
    assert first["planned"] == "2026-10-20T15:46" and first["service_id"] == 24598
    assert second["vehicle"] == "215"
    assert course_sightings(request, {"courseElements": []}) == []
