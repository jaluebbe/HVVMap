import json
from datetime import datetime

from hvv_map import trip_courses
from hvv_map.trip_courses import parse_course, run_trip_courses


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


def test_parse_course_marks_cancelled_stops():
    response = _response()
    response["courseElements"][0]["fromCancelled"] = True
    response["courseElements"][1]["toCancelled"] = True

    stops = parse_course(response)

    assert [s["cancelled"] for s in stops] == [True, False, True]


def test_parse_course_without_elements_is_none():
    assert parse_course({"returnCode": "OK"}) is None


def test_course_of_another_run_does_not_fit_the_request():
    stops = parse_course(_response())
    at = "2026-10-17T09:03:00.000+0200"
    later = "2026-10-17T13:33:00.000+0200"

    assert trip_courses.course_fits_request(stops, {"time": at})
    assert not trip_courses.course_fits_request(stops, {"time": later})


class _Redis:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value

    def lpush(self, key, *values):
        self.lists = getattr(self, "lists", {})
        self.lists[key] = list(reversed(values)) + self.lists.get(key, [])

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists[key][start : end + 1]


class _Client:
    def __init__(self):
        self.requests = []

    def send(self, endpoint, request):
        self.requests.append((endpoint, request))
        return _response()


LINE = {"name": "U1", "dlid": "de:hvv:U1:", "type": {"shortInfo": "U"}}
S_LINE = {"name": "S1", "dlid": "de:hvv:S1:", "type": {"shortInfo": "S"}}
NOW = datetime(2026, 10, 17, 9, 0)


def _dep(service_id, station="A", offset=2, line=LINE):
    return {
        "line": {**line, "direction": "Z"},
        "station": {"id": station, "combinedName": station.lower(), "name": station},
        "timeOffset": offset,
        "directionId": 6,
        "serviceId": service_id,
        "platform": "Gleis 1",
    }


def _redis(departures, cached=None):
    live = {
        "anchor": {"date": "2026-10-17", "time": "09:00:00"},
        "departures": departures,
    }
    values = {"hvv:departures:live": json.dumps({"data": live})}
    if cached is not None:
        values["hvv:trips:courses"] = json.dumps({"data": cached})
    return _Redis(values)


def _stored(redis):
    return json.loads(redis.values["hvv:trips:courses"])["data"]


def _cached(service_id, age=0, station_id="A", request=None):
    return {
        f"live:{service_id}": {
            "fetched_at": trip_courses.time.time() - age,
            "attempts": 0,
            "stops": parse_course(_response()),
            "service_id": str(service_id),
            "station_id": station_id,
            "request": request,
            "vehicles": [],
        }
    }


def test_cancelled_departure_gets_no_course(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    redis = _redis([{**_dep(7), "cancelled": True}, _dep(8, "B")])
    client = _Client()

    assert run_trip_courses(client, redis, NOW) == (1, 0)
    assert [r["serviceId"] for _, r in client.requests] == [8]


def test_course_is_fetched_once_per_live_service_id(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    redis = _redis([_dep(7, "B", 4), _dep(7, "A", 2)])
    client = _Client()

    assert run_trip_courses(client, redis, NOW) == (1, 0)

    request = client.requests[0][1]
    assert request["serviceId"] == 7 and request["lineId"] == "de:hvv:U1:"
    assert request["station"]["name"] == "a"
    assert request["time"].startswith("2026-10-17T09:02:00.000+0200")
    stored = _stored(redis)
    assert list(stored) == ["live:7"] and stored["live:7"]["station_id"] == "A"
    run_trip_courses(client, redis, NOW)
    assert len(client.requests) == 1  # cached, not fetched again


def test_every_live_service_id_gets_its_own_course(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    redis = _redis([_dep(7), _dep(999)])
    client = _Client()

    run_trip_courses(client, redis, NOW)

    assert [r["serviceId"] for _, r in client.requests] == [7, 999]
    assert set(_stored(redis)) == {"live:7", "live:999"}


def test_course_without_a_station_the_id_departs_from_is_refetched_there(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    redis = _redis([_dep(7, "A", 2), _dep(7, "X", 5)], _cached(7))
    client = _Client()

    run_trip_courses(client, redis, NOW)
    assert [r["station"]["name"] for _, r in client.requests] == ["x"]
    assert _stored(redis)["live:7"]["station_id"] == "X"

    run_trip_courses(client, redis, NOW)  # the answer still lacks X: no loop
    assert len(client.requests) == 1


def test_course_of_another_day_is_refetched(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    redis = _redis([_dep(7, "A", 270)], _cached(7))  # 13:30, course runs 09:02
    client = _Client()

    run_trip_courses(client, redis, NOW)

    assert [r["serviceId"] for _, r in client.requests] == [7]


def test_failed_fetch_is_retried_soon_but_only_a_few_times():
    now = trip_courses.time.time()

    def due(age, attempts):
        entry = {"fetched_at": now - age, "stops": None, "attempts": attempts}
        return trip_courses._retry_due(entry, now)

    assert due(150, 1)  # retry after 2 minutes
    assert not due(60, 1)
    assert not due(150, 3)  # then the long pause
    assert due(700, 3)


def test_a_failing_fetch_counts_its_attempts(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    redis = _redis([_dep(555)])

    class _Failing(_Client):
        def send(self, endpoint, request):
            super().send(endpoint, request)
            return {}

    run_trip_courses(_Failing(), redis, NOW)
    cached = _stored(redis)
    assert cached["live:555"]["attempts"] == 1

    cached["live:555"]["fetched_at"] -= 200  # past the short pause
    redis.values["hvv:trips:courses"] = json.dumps({"data": cached})
    run_trip_courses(_Failing(), redis, NOW)
    assert _stored(redis)["live:555"]["attempts"] == 2


def test_http_error_for_one_id_does_not_abort_the_run(monkeypatch):
    import requests

    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    redis = _redis([_dep(1, "A", 2), _dep(2, "A", 3)])

    class _Failing(_Client):
        def send(self, endpoint, request):
            if request["serviceId"] == 1:
                raise requests.HTTPError("500")
            return super().send(endpoint, request)

    assert run_trip_courses(_Failing(), redis, NOW) == (1, 1)
    stored = _stored(redis)
    assert stored["live:1"]["stops"] is None
    assert stored["live:2"]["stops"] is not None


def test_fetch_failing_at_a_station_moves_on_to_the_next_live_departure(
    monkeypatch,
):
    import requests

    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    redis = _redis([_dep(7, "A", 2), _dep(7, "B", 4)])

    class _FailingAtA(_Client):
        def send(self, endpoint, request):
            super().send(endpoint, request)
            if request["station"]["name"] == "a":
                raise requests.HTTPError("500")
            response = _response()
            response["courseElements"] = response["courseElements"][1:]  # from B
            return response

    client = _FailingAtA()
    for _ in range(3):  # no waiting between the runs
        run_trip_courses(client, redis, NOW)

    assert [r["station"]["name"] for _, r in client.requests] == ["a", "b"]
    stored = _stored(redis)["live:7"]
    assert [s["id"] for s in stored["stops"]] == ["B", "C"]
    assert stored["failed_stations"] == ["A"]


def test_course_vehicles_are_recorded_as_sightings(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    recorded = []
    monkeypatch.setattr(
        trip_courses, "record", lambda client, sightings: recorded.extend(sightings)
    )

    class _WithVehicles(_Client):
        def send(self, endpoint, request):
            return {**super().send(endpoint, request), "vehicles": [{"number": "432"}]}

    run_trip_courses(_WithVehicles(), _redis([_dep(7)]), NOW)

    assert [(s["vehicle"], s["service_id"]) for s in recorded] == [("432", 7)]


def test_running_course_is_refreshed_with_its_stored_request_between_anchors():
    cached = _between(5, 3600)
    now_ts = trip_courses.time.time()

    running = trip_courses.course_jobs(
        None, cached, datetime(2026, 10, 17, 9, 10), now_ts
    )
    later = trip_courses.course_jobs(
        None, cached, datetime(2026, 10, 17, 11, 0), now_ts
    )

    assert running == [("live:5", {"serviceId": 5}, "between")]
    assert later == []


def test_missing_courses_come_before_old_ones():
    live = {
        "anchor": {"date": "2026-10-17", "time": "09:00:00"},
        "departures": [_dep(5, line=S_LINE), _dep(6)],
    }
    jobs = trip_courses.course_jobs(
        live, _cached(5, age=3600), NOW, trip_courses.time.time()
    )
    assert [(key, reason) for key, _, reason in jobs] == [
        ("live:6", "new"),
        ("live:5", "age"),
    ]


def _between(service_id, age, delay=0, last="2026-10-17T10:00"):
    stops = [
        {"id": "A", "dep": "2026-10-17T09:00", "arr": None, "dep_delay": delay},
        {
            "id": "B",
            "dep": "2026-10-17T09:30",
            "arr": "2026-10-17T09:29",
            "dep_delay": delay,
        },
        {"id": "C", "dep": None, "arr": last, "arr_delay": delay},
    ]
    return {
        f"live:{service_id}": {
            "fetched_at": trip_courses.time.time() - age,
            "stops": stops,
            "request": {"serviceId": service_id},
        }
    }


def test_refresh_alternates_between_anchors_and_live_then_delayed_first():
    now = datetime(2026, 10, 17, 9, 10)
    live = {
        "anchor": {"date": "2026-10-17", "time": "09:00:00"},
        "departures": [_dep(5, offset=10, line=S_LINE)],
    }
    cached = {
        **_between(5, 4000),  # live, oldest
        **_between(6, 600),  # between anchors
        **_between(7, 900),  # between anchors, older
        **_between(8, 600, delay=300),  # between anchors, delayed
    }
    cached["live:5"]["stops"][0]["id"] = "A"
    jobs = trip_courses.course_jobs(live, cached, now, trip_courses.time.time())
    assert [(key, reason) for key, _, reason in jobs] == [
        ("live:8", "between"),
        ("live:5", "age"),
        ("live:7", "between"),
        ("live:6", "between"),
    ]


def test_course_ending_soon_is_not_refreshed():
    now = datetime(2026, 10, 17, 9, 57)
    jobs = trip_courses.course_jobs(
        None, _between(6, 600), now, trip_courses.time.time()
    )
    assert jobs == []


def test_course_calls_are_logged_with_reason_and_result(monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)
    redis = _redis([_dep(7)])

    run_trip_courses(_Client(), redis, NOW)

    [entry] = [json.loads(raw) for raw in redis.lists["hvv:courses:log"]]
    assert (entry["kind"], entry["reason"], entry["result"]) == ("trip", "new", "ok")
    assert (entry["line"], entry["service_id"], entry["station"]) == ("U1", "7", "a")
    assert entry["stops"] > 0 and entry["delay_change"] is None


def test_delay_change_compares_shared_stops():
    old = [{"id": "A", "dep": "09:00", "dep_delay": 60}, {"id": "B", "arr": "09:05"}]
    new = [
        {"id": "A", "dep": "09:00", "dep_delay": 240},
        {"id": "C", "dep": "09:10", "dep_delay": 900},
    ]
    assert trip_courses.delay_change(old, new) == 180
    assert trip_courses.delay_change(None, new) is None


def test_cache_summary_counts_live_ids_and_trips_between_anchors():
    from hvv_map.course_cache import cache_summary

    now = datetime(2026, 10, 17, 9, 10)
    live = {"departures": [{**_dep(5), "cancelled": True}, _dep(6), _dep(7)]}
    cached = {**_cached(6, age=600), **_between(8, 60)}

    summary = cache_summary(cached, live, now, trip_courses.time.time())

    assert summary["total"]["live"] == 2  # cancelled 5 left out
    assert (summary["total"]["ok"], summary["total"]["missing"]) == (1, 1)
    assert summary["total"]["stale"] == 1
    assert summary["between"]["count"] == 1


LIVE_TS = datetime(2026, 10, 17, 9, 0, tzinfo=trip_courses.BERLIN_TZ).timestamp()


def _u_live(*departures):
    return {
        "anchor": {"date": "2026-10-17", "time": "09:00:00"},
        "departures": list(departures),
    }


def _u_cached(age):
    """U-Bahn course fetched age seconds before the live snapshot."""
    cached = _between(5, 0)
    cached["live:5"].update(fetched_at=LIVE_TS - age, mode="U")
    return cached


def _u_jobs(live, cached, since_live=60):
    now = datetime(2026, 10, 17, 9, 10)
    jobs = trip_courses.course_jobs(live, cached, now, LIVE_TS + since_live)
    return [(key, reason) for key, _, reason in jobs]


def test_u_bahn_course_is_not_refreshed_by_age_alone():
    cached = _u_cached(4000)
    live = _u_live({**_dep(5, offset=0), "delay": 0})

    assert _u_jobs(live, cached) == []
    assert _u_jobs(None, cached) == []  # nor between its anchors


def test_u_bahn_course_is_refetched_when_a_live_departure_is_cancelled():
    cached = _u_cached(5)  # no cooldown for a cancellation
    live = _u_live(_dep(5, offset=0), {**_dep(5, "B", 30), "cancelled": True})

    assert _u_jobs(live, cached, since_live=10) == [("live:5", "cancelled")]


def test_fully_cancelled_u_bahn_trip_refetches_its_course():
    live = _u_live({**_dep(5, offset=0), "cancelled": True})

    assert _u_jobs(live, _u_cached(600)) == [("live:5", "cancelled")]


def test_u_bahn_course_is_refetched_when_the_live_delay_jumps():
    on_time = _u_live({**_dep(5, "B", 30), "delay": 60})
    jumped = _u_live({**_dep(5, "B", 30), "delay": 240})

    assert _u_jobs(on_time, _u_cached(600)) == []
    assert _u_jobs(jumped, _u_cached(600)) == [("live:5", "delay_jump")]


def test_u_bahn_course_is_refetched_when_a_cancelled_stop_runs_again():
    cached = _u_cached(600)
    cached["live:5"]["stops"][1]["cancelled"] = True
    live = _u_live({**_dep(5, "B", 30), "delay": 0})

    assert _u_jobs(live, cached) == [("live:5", "running")]


def test_u_bahn_change_waits_for_the_cooldown_and_newer_live_data():
    live = _u_live({**_dep(5, "B", 30), "delay": 240})

    assert _u_jobs(live, _u_cached(30)) == []  # within the cooldown
    newer_course = _u_cached(-10)  # fetched after the snapshot
    assert _u_jobs(live, newer_course, since_live=600) == []
    newer_cancel = _u_cached(-10)
    cancelled = _u_live({**_dep(5, "B", 30), "cancelled": True})
    assert _u_jobs(cancelled, newer_cancel) == []
