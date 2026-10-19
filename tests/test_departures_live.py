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


def test_fetch_live_departures_one_realtime_request_per_category(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    client = _FakeClient()

    data = fetch_live_departures(client, START)

    assert len(client.requests) == len(live.LIVE_CATEGORIES)
    for (endpoint, request), (stations, types) in zip(
        client.requests, live.LIVE_CATEGORIES
    ):
        assert endpoint == "departureList"
        assert request["useRealtime"] is True
        assert request["maxTimeOffset"] == (
            live.LIVE_WINDOW_MINUTES
            if types == live.MAIN_SERVICE_TYPES
            else live.LIVE_WINDOW_LONG_MINUTES
        )
        assert len(request["stations"]) == len(stations)
        assert request["serviceTypes"] == types
        assert request["time"] == {"date": "2026-10-17", "time": "00:20:00"}
    assert data["anchor"] == {"date": "2026-10-17", "time": "00:20:00"}
    assert data["failed_requests"] == 0


def test_regional_departures_are_filtered_to_wanted_lines(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    monkeypatch.setattr(live, "WANTED_R_BAHN_LINES", {"RE8"})

    data = fetch_live_departures(_FakeClient(), START)

    names = {d["line"]["name"] for d in data["departures"]}
    assert "RE8" in names and "ICE 1" in names  # main category is unfiltered
    ids = {d["serviceId"] for d in data["departures"]}
    assert 100 in ids and 101 not in ids


def test_failed_request_is_counted_and_the_rest_kept(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _: None)

    data = fetch_live_departures(_FakeClient(fail_on={0}), START)

    assert data["failed_requests"] == 1
    assert data["departures"]


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


def test_announcement_stations_add_a_filtered_replacement_bus_category(monkeypatch):
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    client = _FakeClient()

    data = fetch_live_departures(client, START, {"Ohlsdorf"})

    stations, types = client.requests[-1][1]["stations"], client.requests[-1][1]
    assert len(client.requests) == len(live.LIVE_CATEGORIES) + 1
    assert stations == [{"type": "STATION", "name": "Ohlsdorf"}]
    assert types["serviceTypes"] == live.SEV_SERVICE_TYPES
    ids = {d["serviceId"] for d in data["departures"]}
    assert len(client.requests) - 1 in ids  # RE8 of the SEV request passes
    assert 100 + len(client.requests) - 1 not in ids  # "ICE 1" is dropped
