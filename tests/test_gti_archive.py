import json

import pytest

from hvv_map import gti_archive, trip_courses
from hvv_map.departures_live import fetch_live_departures


@pytest.fixture
def archive(monkeypatch, tmp_path):
    conn = gti_archive.connect(str(tmp_path / "gti_live.sqlite"))
    monkeypatch.setattr(gti_archive, "_conn", conn)
    monkeypatch.setattr(gti_archive, "purge_if_due", lambda: None)
    monkeypatch.setattr("time.sleep", lambda _: None)
    return conn


LINE = {
    "id": "HHA-U:U1_HHA-U",
    "dlid": "de:hvv:U1:",
    "name": "U1",
    "direction": "Ohlstedt",
    "origin": "Norderstedt Mitte",
    "type": {"shortInfo": "U"},
}


def _dep(offset=3, delay=0):
    return {
        "line": LINE,
        "station": {"id": "Master:10950", "name": "Hbf", "combinedName": "Hbf"},
        "timeOffset": offset,
        "delay": delay,
        "serviceId": 100000123,  # queried the day before the trip
        "directionId": 1,
        "vehicles": [{"id": "FzgID_012326_5_382", "number": "382"}],
    }


class _Client:
    def __init__(self, departures):
        self.departures = departures

    def send(self, endpoint, request):
        return {
            "returnCode": "OK",
            "time": {"date": "05.11.2026", "time": "08:00"},
            "departures": self.departures,
        }


def test_live_departures_are_archived_in_the_ubahnkarte_format(archive):
    from datetime import datetime

    start = datetime(2026, 11, 5, 8, 0)
    fetch_live_departures(_Client([_dep()]), start)
    fetch_live_departures(_Client([_dep(delay=60)]), start)

    rows = archive.execute("SELECT * FROM departures").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["operating_day"], row["line_key"], row["service_id"]) == (
        "2026-11-05",
        "HHA-U:U1_HHA-U",
        123,
    )
    assert row["dep_time"] == "2026-11-05T08:03:00+01:00"
    assert row["delay_s"] == 60  # the latest state wins
    assert row["vehicle_prefix"] == "012326_5"
    assert json.loads(row["vehicles"]) == ["382"]
    queries = archive.execute("SELECT * FROM queries").fetchall()
    assert {q["endpoint"] for q in queries} == {"departureList"}
    assert all(q["status"] == 200 for q in queries)
    assert row["seen_count"] > 1  # one row, updated per response


def test_course_and_call_are_archived(archive, monkeypatch):
    monkeypatch.setattr(trip_courses.time, "sleep", lambda _: None)

    class _Redis:
        def __init__(self):
            live = {
                "anchor": {"date": "2026-11-05", "time": "08:00:00"},
                "departures": [{**_dep(), "serviceId": 123}],
            }
            self.values = {"hvv:departures:live": json.dumps({"data": live})}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, ex=None):
            self.values[key] = value

        def lpush(self, *args):
            pass

        def ltrim(self, *args):
            pass

    class _CourseClient:
        def send(self, endpoint, request):
            return {
                "returnCode": "OK",
                "courseElements": [
                    {
                        "fromStation": {"id": "Master:10950", "name": "Hbf"},
                        "toStation": {"id": "Master:10951", "name": "X"},
                        "depTime": "2026-11-05T08:03:00.000+0100",
                        "arrTime": "2026-11-05T08:05:00.000+0100",
                    }
                ],
                "vehicles": [{"id": "FzgID_012326_5_382", "number": "382"}],
            }

    monkeypatch.setattr(trip_courses, "record", lambda *a: None)
    monkeypatch.setattr(trip_courses, "course_sightings", lambda *a: [])
    trip_courses.run_trip_courses(_CourseClient(), _Redis())

    course = archive.execute("SELECT * FROM courses").fetchone()
    assert (course["line_key"], course["service_id"], course["trigger"]) == (
        "HHA-U:U1_HHA-U",
        123,
        "new",
    )
    assert (course["first_stop_id"], course["last_stop_id"]) == (
        "Master:10950",
        "Master:10951",
    )
    assert course["vehicle_prefix"] == "012326_5"
    query = archive.execute(
        "SELECT * FROM queries WHERE endpoint = 'departureCourse'"
    ).fetchone()
    assert (query["status"], query["group_name"]) == (200, "U1")


def test_purge_keeps_the_last_days(archive):
    archive.execute(
        "INSERT INTO queries (endpoint, sent_at, status) VALUES"
        " ('departureList', '2026-10-30T08:00:00+01:00', 200),"
        " ('departureList', '2026-11-04T08:00:00+01:00', 200)"
    )
    gti_archive.purge(archive, 4, "2026-11-05")

    assert archive.execute("SELECT count(*) FROM queries").fetchone()[0] == 1


def test_disabled_archive_does_nothing(monkeypatch):
    monkeypatch.setattr(gti_archive, "_conn", None)
    monkeypatch.setenv("GTI_ARCHIVE_DB_PATH", "")

    gti_archive.record_query(endpoint="departureList", sent_at="x", status=200)

    assert gti_archive._conn is None
