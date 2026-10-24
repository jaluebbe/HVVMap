import json
from datetime import datetime

from hvv_map.departures import (
    REDIS_ANCHOR_KEY,
    combine_days,
    _build_request,
    _query_start_time,
    snapshots_stale,
    ANCHOR_STATIONS_MAIN,
    ANCHOR_STATIONS_R,
    _anchor_set,
    _candidate_match_ids,
    _is_subsequence,
    _names_match,
    _subline_candidates,
    group_by_service,
    match_subline,
)


def test_is_subsequence_true_for_ordered_non_contiguous_match():
    assert _is_subsequence(["A", "C"], ["A", "B", "C", "D"])


def test_is_subsequence_false_when_order_is_wrong():
    assert not _is_subsequence(["C", "A"], ["A", "B", "C"])


def test_candidate_match_ids_unchanged_for_a_non_ring_line():
    candidate = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
    assert _candidate_match_ids(candidate, "RE8") == ["A", "B", "C"]


def test_candidate_match_ids_doubles_a_ring_line_candidate():
    # U3's declared arc doesn't loop back to the same id at both ends (it's
    # just one arbitrary cut through the physical loop) - doubling still
    # lets a trip observed starting mid-loop wrap past the declared end
    # back around to the declared start.
    candidate = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
    assert _candidate_match_ids(candidate, "U3") == ["A", "B", "C", "A", "B", "C"]


def test_is_subsequence_matches_ring_line_observed_starting_mid_loop():
    # Real-world case (Barmbek<->Saarlandstraße gap): a trip observed
    # starting at "B", not at the candidate's own declared start "A", wraps
    # past the declared end "C" right back around to "A".
    candidate = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
    ids = _candidate_match_ids(candidate, "U3")
    assert _is_subsequence(["B", "C", "A"], ids)
    assert not _is_subsequence(["B", "C", "A"], [s["id"] for s in candidate])


def test_is_subsequence_rejects_reversed_trip_on_a_non_ring_line():
    # Regression: unconditional doubling would wrongly accept a reversed
    # short trip (e.g. a 2-stop shuttle) as matching the forward candidate.
    candidate = [{"id": "A"}, {"id": "B"}]
    ids = _candidate_match_ids(candidate, "175")
    assert not _is_subsequence(["B", "A"], ids)


def test_subline_candidates_filters_by_line_and_endpoint_names():
    sublines_cache = {
        "U3": {
            "sublines": [
                {
                    "stations": [
                        {"id": "A", "name": "Barmbek"},
                        {"id": "B", "name": "Barmbek"},
                    ]
                },
                {"stations": []},
            ]
        }
    }
    result = _subline_candidates(sublines_cache, "U3", "Barmbek", "Barmbek")
    assert result == [[{"id": "A", "name": "Barmbek"}, {"id": "B", "name": "Barmbek"}]]


def test_anchor_set_by_short_info():
    # departureList reports regional trains as "RB" or "RE", never "R".
    assert _anchor_set({"type": {"shortInfo": "RB"}}) == ANCHOR_STATIONS_R
    assert _anchor_set({"type": {"shortInfo": "RE"}}) == ANCHOR_STATIONS_R
    assert _anchor_set({"type": {"shortInfo": "U"}}) == ANCHOR_STATIONS_MAIN
    assert _anchor_set({"type": {"shortInfo": "Bus"}}) == set()


def _ring_entry(
    observed_station_ids,
    line_id="U3",
    line_name="U3",
    origin="Barmbek",
    destination="Barmbek",
    short_info="U",
):
    return {
        "line": {
            "id": line_id,
            "name": line_name,
            "origin": origin,
            "type": {"shortInfo": short_info},
        },
        "destination": destination,
        "stops": [{"station": {"id": sid}} for sid in observed_station_ids],
    }


def test_match_subline_resolves_ring_line_observed_starting_mid_loop():
    # Regression (real Barmbek<->Saarlandstraße gap): U3's declared arc
    # doesn't loop back to the same id at both ends - it's just one cut
    # through the physical loop - so a trip observed starting partway
    # around it, wrapping past the declared end back to the declared
    # start, must still resolve to that single candidate.
    declared = [
        {"id": "Barmbek", "name": "Barmbek"},
        {"id": "Saarlandstr.", "name": "Saarlandstraße"},
        {"id": "Kellinghusenstr.", "name": "Kellinghusenstraße"},
    ]
    sublines_cache = {"U3": {"sublines": [{"stations": declared}]}}
    entry = _ring_entry(
        ["Saarlandstr.", "Kellinghusenstr.", "Barmbek"],
        destination="Kellinghusenstraße",
    )
    result = match_subline(entry, sublines_cache)
    assert result == [declared]


def test_match_subline_does_not_wrap_around_for_a_non_ring_line():
    # Same shape of observation, but for a line not in RING_LINE_NAMES - no
    # wrap-around is applied, so it must stay unresolved (ambiguous/empty)
    # rather than silently matching a reversed/rotated trip.
    declared = [
        {"id": "A", "name": "A"},
        {"id": "B", "name": "B"},
        {"id": "C", "name": "C"},
    ]
    sublines_cache = {"175": {"sublines": [{"stations": declared}]}}
    entry = _ring_entry(
        ["B", "C", "A"], line_id="175", line_name="175", origin="A", destination="C"
    )
    result = match_subline(entry, sublines_cache)
    assert result == []


def test_match_subline_uses_kuecknitz_anchor_to_tell_re8_variants_apart():
    # Two RE8 variants share origin and destination; only the long one stops
    # at the Lübeck-Kücknitz anchor. A trip without that stop must resolve to
    # the short variant, one with it to the long variant.
    long_variant = [
        {"id": "HBF", "name": "Lübeck Hbf"},
        {"id": "KUE", "name": "Lübeck-Kücknitz"},
        {"id": "STR", "name": "Lübeck-Travemünde Strand"},
    ]
    short_variant = [
        {"id": "HBF", "name": "Lübeck Hbf"},
        {"id": "STR", "name": "Lübeck-Travemünde Strand"},
    ]
    sublines_cache = {
        "RE8": {"sublines": [{"stations": long_variant}, {"stations": short_variant}]}
    }

    def entry(observed):
        return _ring_entry(
            observed,
            line_id="RE8",
            line_name="RE8",
            origin="Lübeck Hbf",
            destination="Lübeck-Travemünde Strand",
            short_info="RE",
        )

    assert match_subline(entry(["HBF"]), sublines_cache) == [short_variant]
    assert match_subline(entry(["HBF", "KUE"]), sublines_cache) == [long_variant]


def test_names_match_tolerates_trailing_qualifier_at_word_boundary():
    assert _names_match("Landungsbrücken", "Landungsbrücken Brücke 1")
    assert _names_match("AIRBUS", "AIRBUS (Schiffsanleger)")
    assert _names_match("Neuhof", "Neuhof")
    assert not _names_match("Altona", "Altonaer Museum")
    assert not _names_match(None, "Neuhof")


def test_match_subline_ferry_short_destination_picks_longest_nested_variant():
    # Departures name the terminus "Landungsbrücken"; one variant skips a
    # stop of the other, and a single observed anchor stop fits both.
    short = [
        {"id": "NEU", "name": "Neuhof"},
        {"id": "MID", "name": "Mitte"},
        {"id": "LB2", "name": "Landungsbrücken Brücke 2"},
    ]
    long = [
        {"id": "NEU", "name": "Neuhof"},
        {"id": "MID", "name": "Mitte"},
        {"id": "EXT", "name": "Extra"},
        {"id": "LB2", "name": "Landungsbrücken Brücke 2"},
    ]
    sublines_cache = {"61": {"sublines": [{"stations": short}, {"stations": long}]}}
    entry = _ring_entry(
        ["NEU"],
        line_id="61",
        line_name="61",
        origin="Neuhof",
        destination="Landungsbrücken",
        short_info="Schiff",
    )
    assert match_subline(entry, sublines_cache) == [long]


def test_match_subline_keeps_non_nested_variants_ambiguous():
    a = [{"id": "S", "name": "S"}, {"id": "X", "name": "X"}, {"id": "E", "name": "E"}]
    b = [{"id": "S", "name": "S"}, {"id": "Y", "name": "Y"}, {"id": "E", "name": "E"}]
    sublines_cache = {"9": {"sublines": [{"stations": a}, {"stations": b}]}}
    entry = _ring_entry(
        ["S"], line_id="9", line_name="9", origin="S", destination="E", short_info="Bus"
    )
    assert len(match_subline(entry, sublines_cache)) == 2


def test_group_by_service_keeps_vehicles_per_stop():
    vehicles = [{"id": "9001", "number": "4711"}]
    departures = [
        {
            "serviceId": 1,
            "line": {"id": "L", "name": "U1", "direction": "X"},
            "timeOffset": 3,
            "station": {"id": "A"},
            "vehicles": vehicles,
        }
    ]
    stops = group_by_service(departures)["1"]["stops"]
    assert stops[0]["vehicles"] == vehicles


def test_berlin_now_follows_berlin_time_regardless_of_container_timezone(monkeypatch):
    import os
    from datetime import datetime
    import time as time_module

    from hvv_map.departures import berlin_now

    monkeypatch.setenv("TZ", "UTC")
    time_module.tzset()
    try:
        utc_now = datetime.now()
        assert 1 <= (berlin_now() - utc_now).total_seconds() / 3600 <= 2.1
    finally:
        monkeypatch.delenv("TZ")
        time_module.tzset()
        os.environ.pop("TZ", None)


def test_query_start_is_midnight_of_the_current_day():
    assert _query_start_time(datetime(2026, 10, 19, 2, 30)) == datetime(2026, 10, 19)


def test_plan_query_ends_before_the_next_midnight():
    request = _build_request({"Altona"}, ["SBAHN"], datetime(2026, 10, 19))
    assert request["maxTimeOffset"] == 1439


class _FakeRedis:
    def __init__(self, data):
        self.data = data

    def get(self, key):
        return self.data.get(key)


def _anchor(day):
    return json.dumps({"data": {"date": day, "time": "00:00:00"}})


def test_snapshots_are_stale_after_midnight_until_rebuilt():
    now = datetime(2026, 10, 19, 0, 5)
    day = "hvv:departures:day:2026-10-19"
    old = _FakeRedis({REDIS_ANCHOR_KEY: _anchor("2026-10-18"), day: "{}"})
    new = _FakeRedis({REDIS_ANCHOR_KEY: _anchor("2026-10-19"), day: "{}"})
    assert snapshots_stale(old, now)
    assert not snapshots_stale(new, now)


def test_snapshots_are_stale_without_todays_day_snapshot():
    now = datetime(2026, 10, 19, 12, 0)
    assert snapshots_stale(_FakeRedis({REDIS_ANCHOR_KEY: _anchor("2026-10-19")}), now)


def test_combine_days_appends_tomorrows_early_hours_as_offsets():
    today = [{"serviceId": "1", "timeOffset": 1430}]
    tomorrow = [
        {"serviceId": "1", "timeOffset": 10},
        {"serviceId": "1", "timeOffset": 24 * 60 - 1},  # beyond the kept tail
    ]
    combined = combine_days(today, tomorrow)
    assert [d["timeOffset"] for d in combined] == [1430, 1450]
    assert tomorrow[0]["timeOffset"] == 10  # input untouched


def test_combine_days_prepends_yesterdays_late_hours_as_negative_offsets():
    yesterday = [
        {"serviceId": "1", "timeOffset": 1430},
        {"serviceId": "1", "timeOffset": 600},  # before the kept tail
    ]
    combined = combine_days([{"serviceId": "1", "timeOffset": 5}], [], yesterday)
    assert [d["timeOffset"] for d in combined] == [-10, 5]
    assert yesterday[0]["timeOffset"] == 1430  # input untouched


class _StoreRedis(_FakeRedis):
    def set(self, key, value, ex=None):
        self.data[key] = value


def test_run_daily_snapshots_fetches_today_and_tomorrow_once(monkeypatch):
    from hvv_map import departures

    calls = []

    def fake_fetch_day(client, redis_client, day):
        calls.append(day)
        return [
            {
                "serviceId": "7",
                "station": {"id": "s"},
                "line": {"name": "RE8", "direction": "X"},
                "timeOffset": 600,
            }
        ]

    monkeypatch.setattr(departures, "fetch_day", fake_fetch_day)
    monkeypatch.setattr(departures.time, "sleep", lambda _: None)
    monkeypatch.setattr(departures, "berlin_now", lambda: datetime(2026, 10, 19, 12))
    redis_client = _StoreRedis({})

    departures.run_daily_snapshots(None, redis_client)
    departures.run_daily_snapshots(None, redis_client)

    assert [d.isoformat() for d in calls] == ["2026-10-19", "2026-10-20"]
    by_service = json.loads(redis_client.data[departures.REDIS_BY_SERVICE_KEY])["data"]
    assert [s["timeOffset"] for s in by_service["7"]["stops"]] == [600]
    assert not departures.snapshots_stale(redis_client, datetime(2026, 10, 19, 12))


def test_to_wall_offsets_converts_elapsed_minutes_across_the_dst_change():
    from datetime import datetime

    from hvv_map.departures import to_wall_offsets

    start = datetime(2026, 10, 25)
    departures = [{"timeOffset": 120}, {"timeOffset": 282}, {"x": 1}]
    # 02:00 summer time, then 03:42 winter time (elapsed 282 min)
    assert to_wall_offsets(departures, start) == [
        {"timeOffset": 120},
        {"timeOffset": 222},
        {"x": 1},
    ]
    assert departures[1]["timeOffset"] == 282  # input untouched


def test_to_wall_offsets_is_a_no_op_on_ordinary_days():
    from datetime import datetime

    from hvv_map.departures import to_wall_offsets

    departures = [{"timeOffset": 0}, {"timeOffset": 1439}]
    assert to_wall_offsets(departures, datetime(2026, 10, 24)) == departures


def test_day_minutes_follows_the_dst_changes():
    from datetime import date

    from hvv_map.departures import day_minutes

    assert day_minutes(date(2026, 10, 24)) == 1440
    assert day_minutes(date(2026, 10, 25)) == 1500
    assert day_minutes(date(2026, 3, 29)) == 1380
