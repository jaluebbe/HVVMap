from datetime import datetime

import hvv_map.departures_live as live
from hvv_map.departures_live import fetch_live_departures

START = datetime(2026, 10, 17, 0, 20, 0)


class _FakeClient:
    def __init__(self, fail_on=()):
        self.requests = []
        self.fail_on = set(fail_on)

    def send(self, endpoint, request):
        index = len(self.requests)
        self.requests.append((endpoint, request))
        if index in self.fail_on:
            return {"returnCode": "ERROR_TEXT"}
        return {
            "returnCode": "OK",
            "departures": [
                {
                    "station": {"id": request["stations"][0]["name"]},
                    "serviceId": index,
                    "timeOffset": 1,
                    "line": {"name": "RE8" if index else "U1"},
                },
                {
                    "station": {"id": "x"},
                    "serviceId": 100 + index,
                    "timeOffset": 2,
                    "line": {"name": "ICE 1"},
                },
            ],
        }


def test_fetch_live_departures_splits_each_category_into_small_requests(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    client = _FakeClient()

    data = fetch_live_departures(client, START)

    expected = []
    for stations, types in live.LIVE_CATEGORIES:
        chunks = -(-len(stations) // live.LIVE_CHUNK_STATIONS)
        expected += [types] * chunks
    assert [r["serviceTypes"] for _, r in client.requests] == expected
    for endpoint, request in client.requests:
        assert endpoint == "departureList"
        assert request["useRealtime"] is True
        assert request["allowCancellations"] is True
        assert 0 < len(request["stations"]) <= live.LIVE_CHUNK_STATIONS
        assert request["maxTimeOffset"] == (
            live.LIVE_WINDOW_MINUTES
            if request["serviceTypes"] == live.MAIN_SERVICE_TYPES
            else live.LIVE_WINDOW_LONG_MINUTES
        )
        assert request["time"] == {"date": "2026-10-17", "time": "00:20:00"}
    main = [
        r for _, r in client.requests if r["serviceTypes"] == live.MAIN_SERVICE_TYPES
    ]
    names = {s["name"] for r in main for s in r["stations"]}
    assert names == live.LIVE_CATEGORIES[0][0]
    assert data["anchor"] == {"date": "2026-10-17", "time": "00:20:00"}
    assert data["failed_requests"] == 0 and data["complete"]


def test_regional_departures_are_filtered_to_wanted_lines(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    monkeypatch.setattr(live, "WANTED_R_BAHN_LINES", {"RE8"})

    client = _FakeClient()
    data = fetch_live_departures(client, START)

    names = {d["line"]["name"] for d in data["departures"]}
    assert "RE8" in names and "ICE 1" in names  # main category is unfiltered
    ids = {d["serviceId"] for d in data["departures"]}
    for index, (_, request) in enumerate(client.requests):
        regional = request["serviceTypes"] == live.R_SERVICE_TYPES
        assert (100 + index in ids) is not regional


def test_failed_request_is_retried_once(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _: None)

    data = fetch_live_departures(_FakeClient(fail_on={0}), START)

    assert data["failed_requests"] == 0 and data["complete"]


def test_failed_main_request_makes_the_snapshot_incomplete(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _: None)

    data = fetch_live_departures(_FakeClient(fail_on={0, 1}), START)

    assert data["failed_requests"] == 1 and not data["complete"]
    assert data["errors"][0].startswith("SBAHN/UBAHN/AKN/FAEHRE 1/")


def test_failed_regional_request_keeps_the_rest(monkeypatch):
    import requests

    monkeypatch.setattr(live.time, "sleep", lambda _: None)

    class _Timeout(_FakeClient):
        def send(self, endpoint, request):
            if request["serviceTypes"] == live.R_SERVICE_TYPES:
                self.requests.append((endpoint, request))
                raise requests.HTTPError("504 Server Error: Gateway Timeout")
            return super().send(endpoint, request)

    data = fetch_live_departures(_Timeout(), START)

    assert data["complete"] and data["departures"]
    assert data["failed_requests"] == -(
        -len(live.ANCHOR_STATIONS_R) // live.LIVE_CHUNK_STATIONS
    )
    assert "504" in data["errors"][0]


class _NoRedis:
    def get(self, key):
        return None


def test_run_live_snapshot_stores_result(monkeypatch):
    stored = {}
    monkeypatch.setattr(
        live,
        "fetch_live_departures",
        lambda client, start, sev: {"departures": [1]},
    )
    monkeypatch.setattr(live, "_store", lambda r, key, data: stored.update({key: data}))
    assert live.run_live_snapshot(object(), _NoRedis()) == {"departures": [1]}
    assert stored == {live.REDIS_LIVE_KEY: {"departures": [1]}}


def test_incomplete_snapshot_is_not_stored(monkeypatch):
    stored = {}
    monkeypatch.setattr(
        live,
        "fetch_live_departures",
        lambda client, start, sev: {"departures": [1], "complete": False},
    )
    monkeypatch.setattr(live, "_store", lambda r, key, data: stored.update({key: data}))
    live.run_live_snapshot(object(), _NoRedis())
    assert stored == {}


def test_announcement_stations_add_a_filtered_replacement_bus_category(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    client = _FakeClient()

    data = fetch_live_departures(client, START, {"Ohlsdorf"})

    stations, types = client.requests[-1][1]["stations"], client.requests[-1][1]
    assert stations == [{"type": "STATION", "name": "Ohlsdorf"}]
    assert types["serviceTypes"] == live.SEV_SERVICE_TYPES
    ids = {d["serviceId"] for d in data["departures"]}
    assert len(client.requests) - 1 in ids  # RE8 of the SEV request passes
    assert 100 + len(client.requests) - 1 not in ids  # "ICE 1" is dropped
