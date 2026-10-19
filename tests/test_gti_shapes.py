import json
from datetime import datetime

from hvv_map.gti_shapes import (
    BERLIN_TZ,
    _concat_coordinates,
    _declared_segment_bridges,
    _departure_query,
    _extract_course,
    get_segment,
    is_resolved,
    load_schedule,
    open_db,
    read_course_log,
    store_schedule,
    is_real_track,
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


def test_subline_hops_uses_the_opposite_direction_reversed(tmp_path):
    conn = open_db(str(tmp_path / "gti_shapes.db"))
    store_segment(conn, "B", "A", [[1, 1], [0.5, 0.5], [0, 0]])
    hops = subline_hops(conn, ("A", "B"))
    assert hops == [("A", "B", [[0, 0], [0.5, 0.5], [1, 1]])]


def test_subline_hops_prefers_own_direction_over_the_opposite(tmp_path):
    conn = open_db(str(tmp_path / "gti_shapes.db"))
    store_segment(conn, "A", "B", [[0, 0], [1, 0]])
    store_segment(conn, "B", "A", [[1, 1], [0, 1]])
    assert subline_hops(conn, ("A", "B")) == [("A", "B", [[0, 0], [1, 0]])]


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


def test_update_gti_shapes_logs_each_call_with_legs_and_delays(tmp_path):
    response = {
        "courseElements": [
            {**e, "model": "DT5", "depDelay": 120, "arrDelay": 300}
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
    assert first_leg["dep_delay"] == 120
    assert first_leg["from"] == "Ohlstedt" and first_leg["extra"] == {"model": "DT5"}
    assert first_leg["dep_time"] and first_leg["arr_time"]
    assert log["calls"][0]["meta"] == {}
    assert log["calls"][0]["segments"] == 2
    assert log["runs"][0]["calls"] == 1 and log["runs"][0]["ok"] == 1


def test_read_course_log_empty_without_table_or_connection(tmp_path):
    import sqlite3

    assert read_course_log(None)["calls"] == []
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    assert read_course_log(bare) == {"calls": [], "runs": []}


def test_update_gti_shapes_keeps_unknown_response_fields(tmp_path):
    response = {
        "returnCode": "OK",
        "courseElements": [
            {**e, "vehicleNumber": "4711"} for e in COURSE_RESPONSE["courseElements"]
        ],
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
    call = read_course_log(conn)["calls"][0]

    assert call["meta"] == {"returnCode": "OK"}
    assert call["legs"][0]["extra"] == {"vehicleNumber": "4711"}


def test_open_db_adds_meta_column_to_an_older_course_log(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE course_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "run_id INTEGER, called_at INTEGER, subline_key TEXT, line_name TEXT, "
        "service_id TEXT, station TEXT, dep_time TEXT, result TEXT, "
        "segments INTEGER, delays_json TEXT)"
    )
    old.commit()
    old.close()

    conn = open_db(path)

    assert "meta_json" in {r[1] for r in conn.execute("PRAGMA table_info(course_log)")}


def test_subline_hops_finds_a_segment_spanning_many_skipped_stops():
    conn = open_db(":memory:")
    ids = tuple("ABCDEFGH")
    store_segment(conn, "A", "H", [[0, 0], [1, 1], [2, 2]])

    assert subline_hops(conn, ids) == [("A", "H", [[0, 0], [1, 1], [2, 2]])]


def test_subline_hops_reverses_a_spanning_segment_of_the_opposite_direction():
    conn = open_db(":memory:")
    store_segment(conn, "H", "A", [[0, 0], [1, 1], [2, 2]])

    assert subline_hops(conn, tuple("ABCDEFGH")) == [
        ("A", "H", [[2, 2], [1, 1], [0, 0]])
    ]


def test_update_gti_shapes_counts_an_http_error_and_continues(tmp_path, monkeypatch):
    import requests

    from hvv_map import gti_shapes

    monkeypatch.setattr(gti_shapes.time, "sleep", lambda _: None)

    class _Failing(_FakeClient):
        def send(self, endpoint, request):
            raise requests.HTTPError("500 Server Error")

    redis_client = _FakeRedis(
        {
            "hvv:departures:by_service": {
                "1": _entry("HHA-U:U1_HHA-U", ("Master:1", "Master:2"), dlid="x")
            },
            "hvv:departures:anchor_start": {"date": "2026-09-21", "time": "04:00:00"},
        }
    )
    conn = open_db(str(tmp_path / "gti_shapes.db"))

    summary = update_gti_shapes(_Failing(None), redis_client, conn)

    assert summary["failed"] == 1 and summary["resolved"] == 0


def test_store_segment_keeps_a_real_track_over_a_straight_fallback():
    conn = open_db(":memory:")
    store_segment(conn, "A", "B", [[0, 0], [0.01, 0.01], [0.02, 0.02]])

    store_segment(conn, "A", "B", [[0, 0], [0.02, 0.02]])

    assert len(get_segment(conn, "A", "B")) == 3


def _service(line_id, station_ids, destination="Z", short_info="Bus", matched=True):
    entry = _entry(line_id, station_ids)
    entry["line"]["type"] = {"shortInfo": short_info}
    entry["destination"] = destination
    entry["stops"] = [
        {"station": {"id": sid, "combinedName": sid}, "timeOffset": 5 + i}
        for i, sid in enumerate(station_ids)
    ]
    if not matched:
        entry["subline"] = None
    return entry


def test_derive_one_course_per_variant_and_replace_a_wrongly_matched_subline(tmp_path):
    from hvv_map.gti_shapes import apply_derived_sublines, derive_unresolved_sublines

    conn = open_db(str(tmp_path / "gti_shapes.db"))
    long_subline = _service("L1", ("A", "B", "C"))  # matched to the long variant
    direct = _service("L1", ("A", "C"))  # observed at its ends only
    direct["subline"] = long_subline["subline"]
    by_service = {
        "1": long_subline,
        "2": _service("L1", ("A", "B", "C")),  # same variant as 1
        "3": direct,
        "4": _service("U1", ("A", "B"), short_info="U"),  # matched rail: untouched
        "5": _service("L2", ("A", "B"), matched=False),
    }
    client = _FakeClient(COURSE_RESPONSE)
    anchor = datetime(2026, 9, 21, 4, 0, tzinfo=BERLIN_TZ)

    summary = derive_unresolved_sublines(client, by_service, anchor, conn)

    assert summary == {"derived": 3, "failed": 0, "remaining": 0}
    assert len(client.sent_requests) == 3  # variants of 1/2, 3 and 5
    assert get_segment(conn, "Master:1", "Master:2")[1] == [10.12, 53.69]
    applied = apply_derived_sublines(conn, by_service)
    assert [s["id"] for s in applied["3"]["subline"]] == [
        "Master:1",
        "Master:2",
        "Master:3",
    ]
    assert [s["id"] for s in applied["4"]["subline"]] == ["A", "B"]
    derive_unresolved_sublines(client, by_service, anchor, conn)
    assert len(client.sent_requests) == 3  # nothing left to fetch


def test_apply_derived_sublines_without_database_or_table_changes_nothing():
    import sqlite3

    from hvv_map.gti_shapes import apply_derived_sublines

    by_service = {"1": _service("L1", ("A", "B"))}

    assert apply_derived_sublines(None, by_service) == by_service
    assert apply_derived_sublines(sqlite3.connect(":memory:"), by_service) == by_service


def test_run_update_skips_derivation_without_anchor(tmp_path):
    from hvv_map.gti_shapes import run_update

    conn = open_db(str(tmp_path / "gti_shapes.db"))

    summary, derived = run_update(_FakeClient({}), _FakeRedis({}), conn)

    assert summary["resolved"] == 0 and derived is None


def test_is_real_track_rejects_two_points_and_long_gaps():
    assert not is_real_track(None)
    assert not is_real_track([[9.0, 53.0], [9.1, 53.0]])
    assert is_real_track([[9.0, 53.0], [9.01, 53.0], [9.02, 53.0]])
    assert not is_real_track([[9.0, 53.0], [9.01, 53.0], [9.2, 53.1]])


def test_store_segment_keeps_gapless_track_over_gappy_one():
    conn = open_db(":memory:")
    good = [[9.0, 53.0], [9.01, 53.0], [9.02, 53.0]]
    store_segment(conn, "A", "B", good)
    store_segment(conn, "A", "B", [[9.0, 53.0], [9.01, 53.0], [9.3, 53.2]])
    assert get_segment(conn, "A", "B") == good


class _Station:
    def __init__(self, lon, lat):
        self.lon, self.lat = lon, lat


def _line_track(n=12):
    return [[9.0 + 0.01 * i, 53.0] for i in range(n)]


def test_clip_track_cuts_between_nearest_vertices_in_both_directions():
    from hvv_map.gti_shapes import clip_track

    path = _line_track()
    assert clip_track(path, (9.02, 53.0), (9.05, 53.0)) == path[2:6]
    assert clip_track(path, (9.05, 53.0), (9.02, 53.0)) == path[2:6][::-1]


def test_clip_track_rejects_stations_off_the_track_and_two_point_parts():
    from hvv_map.gti_shapes import clip_track

    path = _line_track()
    assert clip_track(path, (9.02, 53.0), (9.05, 53.1)) is None
    assert clip_track(path, (9.02, 53.0), (9.03, 53.0)) is None


def test_clipped_hop_tracks_fill_hops_from_a_longer_stored_track():
    from hvv_map.gti_shapes import clipped_hop_tracks

    conn = open_db(":memory:")
    path = _line_track()
    store_segment(conn, "Z", "A", path)  # long leg, stored against the order
    store_segment(conn, "A", "B", [path[3], path[5]])  # straight fallback
    stations = {
        "A": _Station(*path[8]),
        "B": _Station(*path[6]),
        "Z": _Station(*path[0]),
    }

    tracks = {
        (a, b): track
        for a, b, track in clipped_hop_tracks(conn, ("A", "B", "Z"), stations)
    }

    assert tracks[("A", "B")] == path[6:9][::-1]
    assert tracks[("B", "Z")] == path[0:7][::-1]
