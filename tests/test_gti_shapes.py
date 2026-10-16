import json
from datetime import datetime

from hvv_map.gti_shapes import (
    BERLIN_TZ,
    _concat_coordinates,
    _declared_segment_bridges,
    _departure_query,
    _extract_course,
    _representative_entries,
    get_segment,
    is_resolved,
    load_schedule,
    open_db,
    read_course_log,
    store_schedule,
    store_segment,
    subline_key,
    subline_hops,
    update_gti_shapes,
)


def _entry(
    line_id, station_ids, first_station_name="Ohlstedt", offset=5, dlid="de:hvv:U1:"
):
    return {
        "line": {"id": line_id, "dlid": dlid, "name": "U1"},
        "stops": [
            {"station": {"combinedName": first_station_name}, "timeOffset": offset}
        ],
        "subline": [{"id": sid, "name": sid} for sid in station_ids],
    }


def test_subline_key_joins_line_id_and_station_ids():
    assert subline_key("L1", ("A", "B")) == "L1|A|B"


def test_open_db_creates_expected_tables(tmp_path):
    conn = open_db(str(tmp_path / "gti_shapes.db"))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"segments", "subline_schedule"} <= tables


def test_segment_round_trip(tmp_path):
    conn = open_db(str(tmp_path / "gti_shapes.db"))
    assert get_segment(conn, "A", "B") is None
    store_segment(conn, "A", "B", [[9.0, 53.0], [9.1, 53.1]])
    assert get_segment(conn, "A", "B") == [[9.0, 53.0], [9.1, 53.1]]


def test_schedule_round_trip(tmp_path):
    conn = open_db(str(tmp_path / "gti_shapes.db"))
    assert not is_resolved(conn, "L1|A|B")
    assert load_schedule(conn, "L1|A|B") is None
    store_schedule(conn, "L1|A|B", 123, [{"id": "A", "name": "A", "offset": 0}])
    assert is_resolved(conn, "L1|A|B")
    assert load_schedule(conn, "L1|A|B") == [{"id": "A", "name": "A", "offset": 0}]


def test_representative_entries_skips_unresolved_and_dedupes():
    by_service = {
        "1": _entry("L1", ("A", "B")),
        "2": _entry("L1", ("A", "B")),  # same subline, second trip
        "3": {**_entry("L1", ("A", "C")), "subline": None},  # unresolved
    }
    result = _representative_entries(by_service)
    assert list(result.keys()) == ["L1|A|B"]
    assert result["L1|A|B"][0] == "1"


def test_departure_query_builds_iso_time_from_offset():
    entry = _entry("L1", ("A", "B"), first_station_name="Ohlstedt", offset=5)
    anchor = datetime(2026, 9, 21, 4, 0, 0, tzinfo=BERLIN_TZ)
    station_name, iso_time = _departure_query(entry, anchor)
    assert station_name == "Ohlstedt"
    assert iso_time == "2026-09-21T04:05:00.000+0200"


def test_departure_query_none_without_stops():
    assert _departure_query({"stops": []}, datetime.now(tz=BERLIN_TZ)) is None


def test_departure_query_uses_combined_name_not_name():
    # Regression test: departureList's station object has "combinedName",
    # not "name" (that's listLines/StationLight's field) - a stop carrying
    # only "name" (or neither) must not silently produce a query.
    entry = {
        "stops": [{"station": {"name": "Ohlstedt"}, "timeOffset": 5}],
    }
    assert _departure_query(entry, datetime.now(tz=BERLIN_TZ)) is None


COURSE_RESPONSE = {
    "courseElements": [
        {
            "fromStation": {
                "id": "Master:1",
                "name": "Ohlstedt",
                "coordinate": {"x": 10.1, "y": 53.7},
            },
            "toStation": {
                "id": "Master:2",
                "name": "Volksdorf",
                "coordinate": {"x": 10.15, "y": 53.68},
            },
            "depTime": "2026-09-21T04:05:00.000+0200",
            "arrTime": "2026-09-21T04:08:00.000+0200",
            "path": {
                "track": [
                    {"x": 10.1, "y": 53.7},
                    {"x": 10.12, "y": 53.69},
                    {"x": 10.15, "y": 53.68},
                ]
            },
        },
        {
            "fromStation": {
                "id": "Master:2",
                "name": "Volksdorf",
                "coordinate": {"x": 10.15, "y": 53.68},
            },
            "toStation": {
                "id": "Master:3",
                "name": "Buchenkamp",
                "coordinate": {"x": 10.16, "y": 53.67},
            },
            "depTime": "2026-09-21T04:09:00.000+0200",
            "arrTime": "2026-09-21T04:11:00.000+0200",
            "path": {"track": []},
        },
    ]
}


def test_extract_course_segments_use_real_track():
    segments, _ = _extract_course(COURSE_RESPONSE)
    assert segments[0] == (
        "Master:1",
        "Master:2",
        [[10.1, 53.7], [10.12, 53.69], [10.15, 53.68]],
    )


def test_extract_course_falls_back_to_straight_line_without_path():
    segments, _ = _extract_course(COURSE_RESPONSE)
    assert segments[1] == ("Master:2", "Master:3", [[10.15, 53.68], [10.16, 53.67]])


def test_extract_course_stop_offsets_relative_to_first_departure():
    _, stop_offsets = _extract_course(COURSE_RESPONSE)
    assert stop_offsets == [
        {"id": "Master:1", "name": "Ohlstedt", "offset": 0},
        {"id": "Master:2", "name": "Volksdorf", "offset": 180},
        {"id": "Master:3", "name": "Buchenkamp", "offset": 360},
    ]


def test_extract_course_none_without_elements():
    assert _extract_course({"courseElements": []}) is None


def test_concat_coordinates_drops_duplicate_junction_point():
    joined = _concat_coordinates([[[0, 0], [1, 1]], [[1, 1], [2, 2]]])
    assert joined == [[0, 0], [1, 1], [2, 2]]


def test_concat_coordinates_keeps_both_points_without_shared_junction():
    joined = _concat_coordinates([[[0, 0], [1, 1]], [[5, 5], [2, 2]]])
    assert joined == [[0, 0], [1, 1], [5, 5], [2, 2]]


REAL_SEGMENTS = [
    ("Master:1", "Master:2", [[10.1, 53.7], [10.15, 53.68]]),
    ("Master:2", "Master:3", [[10.15, 53.68], [10.16, 53.67]]),
]


def test_declared_segment_bridges_bridges_a_hidden_intermediate_stop():
    # The subline is declared as skipping Master:2 directly (Master:1 ->
    # Master:3), but the real course actually stopped there - the bridge
    # must concatenate both real segments into one Master:1 -> Master:3 path.
    subline = [{"id": "Master:1", "name": "A"}, {"id": "Master:3", "name": "C"}]
    bridges = _declared_segment_bridges(subline, REAL_SEGMENTS)
    assert bridges == [
        ("Master:1", "Master:3", [[10.1, 53.7], [10.15, 53.68], [10.16, 53.67]])
    ]


def test_declared_segment_bridges_handles_reverse_direction():
    subline = [{"id": "Master:3", "name": "C"}, {"id": "Master:1", "name": "A"}]
    bridges = _declared_segment_bridges(subline, REAL_SEGMENTS)
    assert bridges == [
        ("Master:3", "Master:1", [[10.16, 53.67], [10.15, 53.68], [10.1, 53.7]])
    ]


def test_declared_segment_bridges_skips_pair_not_in_real_course():
    subline = [{"id": "Master:1", "name": "A"}, {"id": "Master:unknown", "name": "X"}]
    assert _declared_segment_bridges(subline, REAL_SEGMENTS) == []


def test_declared_segment_bridges_empty_without_real_segments():
    subline = [{"id": "Master:1", "name": "A"}, {"id": "Master:3", "name": "C"}]
    assert _declared_segment_bridges(subline, []) == []


class _FakeRedis:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        value = self.values.get(key)
        return json.dumps({"data": value}) if value is not None else None


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.sent_requests = []

    def send(self, endpoint, request):
        self.sent_requests.append((endpoint, request))
        return self.response


def test_update_gti_shapes_sends_dlid_as_line_id_not_icon_key(tmp_path):
    # Regression test: departureCourse's lineId must be the "dlid" field
    # (e.g. "de:hvv:U1:"), not "id" (the icon lineKey, e.g.
    # "HHA-U:U1_HHA-U") - passing the wrong one made every real call fail.
    by_service = {
        "1": _entry("HHA-U:U1_HHA-U", ("Master:1", "Master:2"), dlid="de:hvv:U1:")
    }
    redis_client = _FakeRedis(
        {
            "hvv:departures:by_service": by_service,
            "hvv:departures:anchor_start": {"date": "2026-09-21", "time": "04:00:00"},
        }
    )
    client = _FakeClient(COURSE_RESPONSE)
    conn = open_db(str(tmp_path / "gti_shapes.db"))

    summary = update_gti_shapes(client, redis_client, conn)

    assert summary == {"resolved": 1, "skipped": 0, "failed": 0, "no_query": 0}
    _, request = client.sent_requests[0]
    assert request["lineId"] == "de:hvv:U1:"


def test_update_gti_shapes_counts_no_query_without_making_a_call(tmp_path):
    # An entry whose first stop has no station/timeOffset can't be turned
    # into a departureCourse query at all - counted separately from an
    # actual failed API call, and no call is made for it.
    by_service = {"1": {**_entry("L1", ("A", "B")), "stops": []}}
    redis_client = _FakeRedis(
        {
            "hvv:departures:by_service": by_service,
            "hvv:departures:anchor_start": {"date": "2026-09-21", "time": "04:00:00"},
        }
    )
    client = _FakeClient(COURSE_RESPONSE)
    conn = open_db(str(tmp_path / "gti_shapes.db"))

    summary = update_gti_shapes(client, redis_client, conn)

    assert summary == {"resolved": 0, "skipped": 0, "failed": 1, "no_query": 1}
    assert client.sent_requests == []


def test_update_gti_shapes_stores_bridge_for_declared_pair_with_hidden_stop(tmp_path):
    # Regression: a subline declared as skipping an intermediate stop, whose
    # representative trip's real course actually served it anyway, must
    # still get a stored segment for the declared (skipping) pair itself -
    # not just the real intermediate legs - see _declared_segment_bridges.
    by_service = {
        "1": _entry("HHA-U:U1_HHA-U", ("Master:1", "Master:3"), dlid="de:hvv:U1:")
    }
    redis_client = _FakeRedis(
        {
            "hvv:departures:by_service": by_service,
            "hvv:departures:anchor_start": {"date": "2026-09-21", "time": "04:00:00"},
        }
    )
    client = _FakeClient(COURSE_RESPONSE)
    conn = open_db(str(tmp_path / "gti_shapes.db"))

    update_gti_shapes(client, redis_client, conn)

    assert get_segment(conn, "Master:1", "Master:2") is not None  # real leg
    assert get_segment(conn, "Master:2", "Master:3") is not None  # real leg
    assert get_segment(conn, "Master:1", "Master:3") == [
        [10.1, 53.7], [10.12, 53.69], [10.15, 53.68], [10.16, 53.67]
    ]  # fmt: skip


def test_subline_hops_reuses_a_segment_spanning_a_skipped_stop(tmp_path):
    conn = open_db(str(tmp_path / "gti_shapes.db"))
    store_segment(conn, "A", "C", [[0, 0], [1, 1], [2, 2]])
    hops = subline_hops(conn, ("A", "B", "C", "D"))
    assert hops == [("A", "C", [[0, 0], [1, 1], [2, 2]]), ("C", "D", None)]


def test_subline_hops_without_connection_has_no_coordinates():
    assert subline_hops(None, ("A", "B")) == [("A", "B", None)]


def test_update_gti_shapes_tries_more_trips_while_hops_lack_a_segment(
    tmp_path, monkeypatch
):
    # The course never serves Master:9, so its hops stay uncovered: the first
    # trip plus EXTRA_COURSE_TRIES further ones are queried, no more.
    monkeypatch.setattr("hvv_map.gti_shapes.time.sleep", lambda _: None)
    by_service = {
        str(i): _entry("L1", ("Master:1", "Master:9", "Master:3"), dlid="de:hvv:L1:")
        for i in range(1, 9)
    }
    redis_client = _FakeRedis(
        {
            "hvv:departures:by_service": by_service,
            "hvv:departures:anchor_start": {"date": "2026-09-21", "time": "04:00:00"},
        }
    )
    client = _FakeClient(COURSE_RESPONSE)
    conn = open_db(str(tmp_path / "gti_shapes.db"))

    summary = update_gti_shapes(client, redis_client, conn)

    assert summary["resolved"] == 1
    assert len(client.sent_requests) == 4


def test_update_gti_shapes_logs_each_call_with_delays(tmp_path):
    response = {
        "courseElements": [
            {**e, "depDelay": 120, "arrDelay": 300}
            for e in COURSE_RESPONSE["courseElements"]
        ]
    }
    by_service = {
        "1": _entry("HHA-U:U1_HHA-U", ("Master:1", "Master:2"), dlid="de:hvv:U1:")
    }
    redis_client = _FakeRedis(
        {
            "hvv:departures:by_service": by_service,
            "hvv:departures:anchor_start": {"date": "2026-09-21", "time": "04:00:00"},
        }
    )
    conn = open_db(str(tmp_path / "gti_shapes.db"))

    update_gti_shapes(_FakeClient(response), redis_client, conn)
    log = read_course_log(conn)

    assert [c["result"] for c in log["calls"]] == ["ok"]
    assert log["calls"][0]["max_delay_s"] == 300
    first_leg = log["calls"][0]["legs"][0]
    assert first_leg["from"] == "Ohlstedt" and first_leg["dep_delay"] == 120
    assert first_leg["dep_time"] and first_leg["arr_time"]
    assert log["calls"][0]["segments"] == 2
    assert log["runs"][0]["calls"] == 1 and log["runs"][0]["ok"] == 1


def test_read_course_log_empty_without_table_or_connection(tmp_path):
    import sqlite3

    assert read_course_log(None)["calls"] == []
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    assert read_course_log(bare) == {"calls": [], "runs": []}
