from hvv_map.departures import (
    ANCHOR_STATIONS_MAIN,
    ANCHOR_STATIONS_R,
    _anchor_set,
    _candidate_match_ids,
    _is_subsequence,
    _subline_candidates,
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
