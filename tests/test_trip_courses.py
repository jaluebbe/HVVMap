import json
from datetime import datetime

from hvv_map import trip_courses
from hvv_map.trip_courses import due_trips, parse_course, run_trip_courses


def _response():
    return {
        "returnCode": "OK",
        "courseElements": [
            {
                "fromStation": {"id": "A", "name": "a"},
                "toStation": {"id": "B", "name": "b"},
                "depTime": "2026-10-17T09:02:00.000+0200",
                "arrTime": "2026-10-17T09:04:00.000+0200",
                "depDelay": 60,
                "arrDelay": 60,
                "fromPlatform": "Gleis 1",
            },
            {
                "fromStation": {"id": "B", "name": "b"},
                "toStation": {"id": "C", "name": "c"},
                "depTime": "2026-10-17T09:04:00.000+0200",
                "arrTime": "2026-10-17T09:07:00.000+0200",
            },
        ],
    }


def test_parse_course_gives_one_stop_per_station_with_local_times():
    stops = parse_course(_response())

    assert [s["id"] for s in stops] == ["A", "B", "C"]
    assert stops[0]["dep"] == "2026-10-17T09:02" and stops[0]["arr"] is None
    assert (
        stops[1]["arr"] == "2026-10-17T09:04" and stops[1]["dep"] == "2026-10-17T09:04"
    )
    assert stops[2]["dep"] is None and stops[2]["arr"] == "2026-10-17T09:07"
    assert stops[0]["dep_delay"] == 60 and stops[0]["platform"] == "Gleis 1"


def test_parse_course_without_elements_is_none():
    assert parse_course({"returnCode": "OK"}) is None


def _trip(service_id, minute):
    return {
        "service_id": service_id,
        "line_obj": {"dlid": "de:hvv:U1:"},
        "stops": [{"station": "a", "planned": datetime(2026, 10, 17, 9, minute)}],
    }


def test_due_trips_skips_cached_and_out_of_range_trips_soonest_first():
    now = datetime(2026, 10, 17, 9, 0)
    trips = [_trip("2", 30), _trip("1", 10), _trip("3", 15), _trip("4", 59)]
    trips[3]["stops"][0]["planned"] = datetime(2026, 10, 17, 12, 0)
    cached = {trip_courses.course_key(trips[2]): {"fetched_at": 0, "stops": [{}]}}

    assert [t["service_id"] for t in due_trips(trips, cached, now)] == ["1", "2"]


class _Redis:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value


class _Client:
    def __init__(self):
        self.requests = []

    def send(self, endpoint, request):
        self.requests.append((endpoint, request))
        return _response()


def test_run_trip_courses_fetches_missing_course_and_stores_it(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    plan = {
        "7": {
            "line": {"name": "U1", "dlid": "de:hvv:U1:", "type": {"shortInfo": "U"}},
            "directionId": 6,
            "destination": "Z",
            "stops": [{"timeOffset": 302, "station": {"id": "A", "combinedName": "a"}}],
        }
    }
    redis = _Redis(
        {
            "hvv:departures:by_service": json.dumps({"data": plan}),
            "hvv:departures:anchor_start": json.dumps(
                {"data": {"date": "2026-10-17", "time": "04:00:00"}}
            ),
        }
    )
    client = _Client()

    fetched, failed = run_trip_courses(client, redis, datetime(2026, 10, 17, 9, 0))

    assert (fetched, failed) == (1, 0)
    request = client.requests[0][1]
    assert request["serviceId"] == 7 and request["lineId"] == "de:hvv:U1:"
    assert request["time"].startswith("2026-10-17T09:02:00.000+0200")
    stored = json.loads(redis.values["hvv:trips:courses"])["data"]
    assert list(stored) == ["7@2026-10-17T09:02"]

    run_trip_courses(client, redis, datetime(2026, 10, 17, 9, 0))
    assert len(client.requests) == 1  # cached, not fetched again


def test_failed_fetch_is_not_retried_immediately_and_not_used(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    trip = _trip("1", 10)
    key = trip_courses.course_key(trip)
    now = datetime(2026, 10, 17, 9, 0)
    fresh = {key: {"fetched_at": trip_courses.time.time(), "stops": None}}
    stale = {key: {"fetched_at": trip_courses.time.time() - 3600, "stops": None}}

    assert due_trips([trip], fresh, now) == []
    assert due_trips([trip], stale, now) == [trip]
