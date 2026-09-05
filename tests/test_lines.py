from unittest.mock import MagicMock

from hvv_map.lines import (
    LineInfo,
    by_name,
    fetch_lines,
    fetch_sublines,
    is_line_of_interest,
    lines_of_interest,
    load_lines,
    simple_types_used,
    store_lines,
)
from hvv_map.redis_client import get_redis_client

FAKE_RESPONSE = {
    "lines": [
        {
            "id": "HHA-U:U1_HHA-U",
            "name": "U1",
            "carrierNameShort": "Hochbahn",
            "type": {"simpleType": "U_BAHN", "shortInfo": "U-Bahn"},
        },
        {
            "id": "HHA-B:U1-ERSATZ_HHA-B",
            "name": "U1-ERSATZ",
            "carrierNameShort": "Hochbahn",
            "type": {"simpleType": "BUS", "shortInfo": "Bus"},
        },
        {"id": "x", "name": "no-type-line", "carrierNameShort": "x"},
    ]
}


def _client_with(response):
    client = MagicMock()
    client.send.return_value = response
    return client


def test_fetch_lines_parses_all_fields():
    lines = fetch_lines(_client_with(FAKE_RESPONSE))
    assert lines[0] == LineInfo(
        id="HHA-U:U1_HHA-U",
        name="U1",
        carrier_short="Hochbahn",
        simple_type="U_BAHN",
    )


def test_fetch_lines_handles_missing_type():
    lines = fetch_lines(_client_with(FAKE_RESPONSE))
    assert lines[2].simple_type == ""


def test_fetch_lines_sends_empty_data_release_id():
    client = _client_with(FAKE_RESPONSE)
    fetch_lines(client)
    request = client.send.call_args[0][1]
    assert request["dataReleaseID"] == ""


def test_by_name():
    lines = fetch_lines(_client_with(FAKE_RESPONSE))
    index = by_name(lines)
    assert index["U1"].id == "HHA-U:U1_HHA-U"
    assert "U1-ERSATZ" in index


def test_simple_types_used_excludes_blank():
    lines = fetch_lines(_client_with(FAKE_RESPONSE))
    assert simple_types_used(lines) == {"U_BAHN", "BUS"}


def test_is_line_of_interest_matches_u_s_akn_and_their_replacement_buses():
    for name in ["U1", "S3", "A2", "U1-ERSATZ", "S3-SEV", "A3-Bus"]:
        assert is_line_of_interest(
            LineInfo(id="x", name=name, carrier_short="", simple_type="")
        )


def test_is_line_of_interest_matches_ferries_by_carrier_not_name():
    line = LineInfo(id="x", name="62", carrier_short="HADAG", simple_type="SCHIFF")
    assert is_line_of_interest(line)


def test_is_line_of_interest_excludes_unrelated_lines():
    for name in ["1", "20", "RE7", "Metrobus 5", ""]:
        line = LineInfo(id="x", name=name, carrier_short="VHH", simple_type="BUS")
        assert not is_line_of_interest(line)


def test_lines_of_interest_filters_the_full_list():
    lines = [
        LineInfo(id="a", name="U1", carrier_short="Hochbahn", simple_type="U_BAHN"),
        LineInfo(
            id="b", name="Metrobus 5", carrier_short="Hochbahn", simple_type="BUS"
        ),
        LineInfo(id="c", name="61", carrier_short="HADAG", simple_type="SCHIFF"),
    ]
    selected = lines_of_interest(lines)
    assert {line.id for line in selected} == {"a", "c"}


def test_store_and_load_lines_roundtrip():
    redis_client = get_redis_client()
    lines = [
        LineInfo(id="a", name="U1", carrier_short="Hochbahn", simple_type="U_BAHN"),
        LineInfo(id="c", name="61", carrier_short="HADAG", simple_type="SCHIFF"),
    ]
    store_lines(redis_client, lines)
    loaded = load_lines(redis_client)
    assert loaded == lines


def test_load_lines_returns_empty_list_when_nothing_stored():
    redis_client = get_redis_client()
    redis_client.delete("hvv:lines")
    assert load_lines(redis_client) == []


FAKE_SUBLINE_RESPONSE = {
    "lines": [
        {
            "id": "HHA-U:U1_HHA-U",
            "name": "U1",
            "carrierNameShort": "Hochbahn",
            "type": {"simpleType": "U_BAHN"},
            "sublines": [
                {
                    "sublineNumber": "3",
                    "vehicleType": "U_BAHN",
                    "stationSequence": [
                        {"id": "Master:1", "name": "Ohlstedt"},
                        {"id": "Master:2", "name": "Volksdorf"},
                    ],
                },
                {
                    "sublineNumber": "7",
                    "vehicleType": "U_BAHN",
                    "stationSequence": [
                        {"id": "Master:2", "name": "Volksdorf"},
                        {"id": "Master:3", "name": "Großhansdorf"},
                    ],
                },
            ],
        },
        {
            "id": "x",
            "name": "Metrobus 5",  # not a line of interest
            "carrierNameShort": "Hochbahn",
            "type": {"simpleType": "BUS"},
            "sublines": [
                {
                    "sublineNumber": "1",
                    "vehicleType": "METROBUS",
                    "stationSequence": [{"id": "Master:9", "name": "Irrelevant"}],
                }
            ],
        },
    ]
}


def _client_with_sublines(response):
    client = MagicMock()
    client.send.return_value = response
    return client


def test_fetch_sublines_parses_station_sequence():
    sublines = fetch_sublines(_client_with_sublines(FAKE_SUBLINE_RESPONSE))
    assert sublines[0].line_name == "U1"
    assert sublines[0].line_id == "HHA-U:U1_HHA-U"
    assert sublines[0].vehicle_type == "U_BAHN"
    assert sublines[0].station_ids == ("Master:1", "Master:2")


def test_fetch_sublines_returns_all_sublines_of_a_line():
    sublines = fetch_sublines(_client_with_sublines(FAKE_SUBLINE_RESPONSE))
    u1_sublines = [s for s in sublines if s.line_name == "U1"]
    assert len(u1_sublines) == 2


def test_fetch_sublines_filters_out_uninteresting_lines():
    sublines = fetch_sublines(_client_with_sublines(FAKE_SUBLINE_RESPONSE))
    assert all(s.line_name != "Metrobus 5" for s in sublines)


def test_fetch_sublines_sends_with_sublines_flag():
    client = _client_with_sublines(FAKE_SUBLINE_RESPONSE)
    fetch_sublines(client)
    request = client.send.call_args[0][1]
    assert request["withSublines"] is True
