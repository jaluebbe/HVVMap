import time
from unittest.mock import MagicMock, patch

import pytest

import hvv_map.segment_cache as sc


@pytest.fixture
def conn(tmp_path):
    connection = sc.open_cache(str(tmp_path / "cache.db"))
    yield connection
    connection.close()


def _client_with(response):
    client = MagicMock()
    client.send.return_value = response
    return client


def test_store_and_get_cached_roundtrip(conn):
    sc.store(conn, ("A", "B"), [9.9, 53.5, 9.91, 53.51])
    assert sc.get_cached(conn, ("A", "B")) == [9.9, 53.5, 9.91, 53.51]


def test_get_cached_returns_none_when_missing(conn):
    assert sc.get_cached(conn, ("X", "Y")) is None


def test_all_cache_misses_batched_into_one_call(conn):
    client = _client_with(
        {
            "trackIDs": ["A*B", "C*D"],
            "tracks": [
                {"track": [1.0, 2.0], "coordinateType": "EPSG_4326"},
                {"track": [3.0, 4.0], "coordinateType": "EPSG_4326"},
            ],
        }
    )
    result = sc.get_or_fetch_tracks(conn, client, [("A", "B"), ("C", "D")])
    assert result == {("A", "B"): [1.0, 2.0], ("C", "D"): [3.0, 4.0]}
    assert client.send.call_count == 1


def test_full_cache_hit_makes_no_api_call(conn):
    sc.store(conn, ("A", "B"), [1.0, 2.0])
    client = _client_with({"trackIDs": [], "tracks": []})
    result = sc.get_or_fetch_tracks(conn, client, [("A", "B")])
    assert result == {("A", "B"): [1.0, 2.0]}
    assert client.send.call_count == 0


def test_mixed_hit_and_miss_only_fetches_missing(conn):
    sc.store(conn, ("A", "B"), [1.0, 2.0])
    client = _client_with(
        {
            "trackIDs": ["E*F"],
            "tracks": [{"track": [5.0, 6.0], "coordinateType": "EPSG_4326"}],
        }
    )
    result = sc.get_or_fetch_tracks(conn, client, [("A", "B"), ("E", "F")])
    assert result == {("A", "B"): [1.0, 2.0], ("E", "F"): [5.0, 6.0]}
    assert client.send.call_args[0][1]["stopPointKeys"] == ["E", "F"]


def test_fetched_segments_are_persisted_for_next_call(conn):
    client = _client_with(
        {
            "trackIDs": ["A*B"],
            "tracks": [{"track": [1.0, 2.0], "coordinateType": "EPSG_4326"}],
        }
    )
    sc.get_or_fetch_tracks(conn, client, [("A", "B")])
    assert sc.get_cached(conn, ("A", "B")) == [1.0, 2.0]


def test_record_and_get_line_usage(conn):
    sc.record_line_usage(conn, ("A", "B"), "U1", "U")
    assert sc.get_lines_for_segment(conn, ("A", "B")) == [("U1", "U")]


def test_record_line_usage_multiple_lines_on_same_segment(conn):
    sc.record_line_usage(conn, ("A", "B"), "U1", "U")
    sc.record_line_usage(conn, ("A", "B"), "U1-ERSATZ", "U")
    lines = sc.get_lines_for_segment(conn, ("A", "B"))
    assert set(lines) == {("U1", "U"), ("U1-ERSATZ", "U")}


def test_record_line_usage_is_idempotent(tmp_path):
    conn = sc.open_cache(str(tmp_path / "cache.db"))
    sc.record_line_usage(conn, ("A", "B"), "U1", "U")
    sc.record_line_usage(conn, ("A", "B"), "U1", "U")  # same call twice
    assert sc.get_lines_for_segment(conn, ("A", "B")) == [("U1", "U")]
    conn.close()


def test_record_line_usage_skips_empty_line_name(conn):
    sc.record_line_usage(conn, ("A", "B"), "", "U")
    assert sc.get_lines_for_segment(conn, ("A", "B")) == []


def test_get_lines_for_segment_empty_when_never_recorded(conn):
    assert sc.get_lines_for_segment(conn, ("X", "Y")) == []


def test_all_segments_with_lines_includes_only_segments_with_recorded_lines(conn):
    sc.store(conn, ("A", "B"), [1.0, 2.0])
    sc.store(conn, ("C", "D"), [3.0, 4.0])  # geometry only, no line recorded
    sc.record_line_usage(conn, ("A", "B"), "U1", "U")

    result = sc.all_segments_with_lines(conn)
    assert len(result) == 1
    key, track, lines = result[0]
    assert key == ("A", "B")
    assert track == [1.0, 2.0]
    assert lines == [("U1", "U")]


def test_record_line_usage_updates_last_seen_on_repeat_calls(tmp_path):
    conn = sc.open_cache(str(tmp_path / "cache.db"))
    sc.record_line_usage(conn, ("A", "B"), "U1", "U", seen_at=1000)
    sc.record_line_usage(conn, ("A", "B"), "U1", "U", seen_at=2000)
    row = conn.execute(
        "SELECT last_seen FROM segment_lines WHERE start_key='A' AND end_key='B'"
    ).fetchone()
    assert row[0] == 2000
    conn.close()


def test_get_lines_for_segment_excludes_stale_pairing(conn):
    sc.record_line_usage(conn, ("A", "B"), "U1", "U", seen_at=1000)
    assert sc.get_lines_for_segment(conn, ("A", "B"), min_last_seen=1500) == []
    assert sc.get_lines_for_segment(conn, ("A", "B"), min_last_seen=500) == [
        ("U1", "U")
    ]


def test_all_segments_with_lines_excludes_stale_segment(tmp_path):
    conn = sc.open_cache(str(tmp_path / "cache.db"))
    sc.store(conn, ("A", "B"), [1.0, 2.0])
    sc.record_line_usage(conn, ("A", "B"), "U1", "U", seen_at=1000)

    now = 1000 + 7 * 24 * 60 * 60  # 7 days later, past the 6-day default threshold
    with patch.object(time, "time", return_value=now):
        result = sc.all_segments_with_lines(conn)
    assert result == []
    conn.close()


def test_all_segments_with_lines_keeps_fresh_segment(tmp_path):
    conn = sc.open_cache(str(tmp_path / "cache.db"))
    sc.store(conn, ("A", "B"), [1.0, 2.0])
    sc.record_line_usage(conn, ("A", "B"), "U1", "U", seen_at=1000)

    now = 1000 + 1 * 60 * 60  # 1h later, well within the 48h threshold
    with patch.object(time, "time", return_value=now):
        result = sc.all_segments_with_lines(conn)
    assert len(result) == 1
    conn.close()


def test_all_segments_with_lines_drops_only_stale_line_keeps_fresh_one(tmp_path):
    conn = sc.open_cache(str(tmp_path / "cache.db"))
    sc.store(conn, ("A", "B"), [1.0, 2.0])
    sc.record_line_usage(conn, ("A", "B"), "U1", "U", seen_at=1000)  # stale
    sc.record_line_usage(
        conn, ("A", "B"), "U1-ERSATZ", "U", seen_at=1000 + 7 * 24 * 60 * 60
    )

    now = 1000 + 7 * 24 * 60 * 60  # 7 days later, past the 6-day default threshold
    with patch.object(time, "time", return_value=now):
        result = sc.all_segments_with_lines(conn)
    assert len(result) == 1
    _key, _track, lines = result[0]
    assert lines == [("U1-ERSATZ", "U")]
    conn.close()


def test_active_station_names_includes_fresh_station(tmp_path):
    conn = sc.open_cache(str(tmp_path / "cache.db"))
    sc.record_station_seen(conn, "Ohlstedt", seen_at=1000)

    now = 1000 + 60  # 1 minute later, well within any reasonable threshold
    with patch.object(time, "time", return_value=now):
        active = sc.active_station_names(conn)
    assert "Ohlstedt" in active
    conn.close()


def test_active_station_names_excludes_stale_station(tmp_path):
    conn = sc.open_cache(str(tmp_path / "cache.db"))
    sc.record_station_seen(conn, "Ohlstedt", seen_at=1000)

    now = 1000 + 7 * 24 * 60 * 60  # 7 days later, past the 6-day default threshold
    with patch.object(time, "time", return_value=now):
        active = sc.active_station_names(conn)
    assert "Ohlstedt" not in active
    conn.close()


def test_record_station_seen_updates_last_seen_on_repeat_calls(tmp_path):
    conn = sc.open_cache(str(tmp_path / "cache.db"))
    sc.record_station_seen(conn, "Ohlstedt", seen_at=1000)
    sc.record_station_seen(conn, "Ohlstedt", seen_at=2000)
    row = conn.execute(
        "SELECT last_seen FROM station_activity WHERE station_name='Ohlstedt'"
    ).fetchone()
    assert row[0] == 2000
    conn.close()


def test_record_station_seen_skips_empty_name(conn):
    sc.record_station_seen(conn, "", seen_at=1000)
    assert sc.active_station_names(conn) == set()
