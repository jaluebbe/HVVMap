from hvv_map.geojson import build_positions_geojson

TRACK = {"track": [10.0, 53.5, 10.001, 53.5, 10.002, 53.5]}


def _journey(
    vehicle_type, line_id, name, destination, model="", journey_suffix="", delay=0
):
    return {
        "journeyID": f"{line_id}.test{journey_suffix}",
        "line": {"id": line_id, "name": name, "type": {"model": model}},
        "vehicleType": vehicle_type,
        "segments": [
            {
                "startDateTime": 1000,
                "endDateTime": 1020,
                "track": TRACK,
                "destination": destination,
                "realtimeDelay": delay,
            },
        ],
    }


def test_builds_one_feature_per_journey():
    data = {
        "journeys": [
            _journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt", "DT5"),
            _journey(
                "SCHIFF", "ZVU-DB:62_ZVU-DB_HADAGZ", "62", "Finkenwerder", "Fähre"
            ),
        ]
    }
    result = build_positions_geojson(data, now_ts=1010)
    assert result["type"] == "FeatureCollection"
    assert len(result["features"]) == 2


def test_feature_geometry_and_progress():
    data = {"journeys": [_journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt", "DT5")]}
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Point"
    assert feature["properties"]["progress"] == 0.5


def test_mode_from_vehicle_type():
    for vehicle_type, expected_mode in [
        ("U_BAHN", "U"),
        ("S_BAHN", "S"),
        ("A_BAHN", "AKN"),
        ("SCHIFF", "FERRY"),
    ]:
        data = {"journeys": [_journey(vehicle_type, "some:id", "X", "Y")]}
        feature = build_positions_geojson(data, now_ts=1010)["features"][0]
        assert feature["properties"]["mode"] == expected_mode


def test_mode_for_known_replacement_bus():
    data = {
        "journeys": [
            _journey("REGIONALBUS", "HHA-B:U1-ERSATZ_HHA-B", "U1-ERSATZ", "Lattenkamp")
        ]
    }
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert feature["properties"]["mode"] == "U"


def test_icon_url_uses_line_id_directly():
    data = {"journeys": [_journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt")]}
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert "lineKey=HHA-U:U1_HHA-U" in feature["properties"]["icon"]


def test_label_strips_parenthetical_suffix():
    data = {
        "journeys": [
            _journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Lattenkamp (Sporthalle)")
        ]
    }
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert feature["properties"]["label"] == "Lattenkamp"


def test_text_combines_destination_and_model():
    data = {"journeys": [_journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt", "DT5")]}
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert feature["properties"]["text"] == "Ohlstedt<br>DT5"


def test_journey_without_usable_track_is_skipped():
    journey = _journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt")
    journey["segments"] = [{"startDateTime": 1000, "endDateTime": 1020}]  # no track
    result = build_positions_geojson({"journeys": [journey]}, now_ts=1010)
    assert result["features"] == []


def test_empty_journeys_yields_empty_feature_collection():
    result = build_positions_geojson({"journeys": []}, now_ts=1010)
    assert result == {"type": "FeatureCollection", "features": []}


def test_uses_segment_destination_not_line_direction():
    journey = _journey("S_BAHN", "ZVU-DB:S1_ZVU-DB_SBHZVU", "S1", "Wedel")
    feature = build_positions_geojson({"journeys": [journey]}, now_ts=1010)["features"][
        0
    ]
    assert feature["properties"]["label"] == "Wedel"


def _s1_journey(journey_id, direction, destination, start_station="A", end_station="B"):
    return {
        "journeyID": journey_id,
        "line": {"id": "ZVU-DB:S1_ZVU-DB_SBHZVU", "name": "S1", "direction": direction},
        "vehicleType": "S_BAHN",
        "segments": [
            {
                "startDateTime": 1000,
                "endDateTime": 1020,
                "track": TRACK,
                "destination": destination,
                "startStationName": start_station,
                "endStationName": end_station,
            },
        ],
    }


def test_s1_kept_when_segment_starts_at_airport():
    journey = _s1_journey(
        "id.no-realtime", "Wedel", "Wedel", start_station="Hamburg Airport (Flughafen)"
    )
    result = build_positions_geojson({"journeys": [journey]}, now_ts=1010)
    assert len(result["features"]) == 1


def test_s1_kept_when_segment_ends_at_airport():
    journey = _s1_journey(
        "id.no-realtime", "Wedel", "Wedel", end_station="Hamburg Airport (Flughafen)"
    )
    result = build_positions_geojson({"journeys": [journey]}, now_ts=1010)
    assert len(result["features"]) == 1


def test_s1_realtime_id_kept_when_destination_equals_direction():
    journey = _s1_journey("id.REALTIME.test", "Poppenbüttel", "Poppenbüttel")
    result = build_positions_geojson({"journeys": [journey]}, now_ts=1010)
    assert len(result["features"]) == 1


def test_s1_realtime_id_kept_when_direction_is_substring_of_destination():
    journey = _s1_journey(
        "id.REALTIME.test", "Poppenbüttel", "Poppenbüttel / Hamburg Airport (Flughafen)"
    )
    result = build_positions_geojson({"journeys": [journey]}, now_ts=1010)
    assert len(result["features"]) == 1


def test_s1_realtime_id_dropped_when_direction_not_in_destination():
    # Der bestaetigte Blankenese/Wedel-Fall.
    journey = _s1_journey("id.REALTIME.test", "Blankenese", "Wedel")
    result = build_positions_geojson({"journeys": [journey]}, now_ts=1010)
    assert result["features"] == []


def test_s1_non_realtime_dropped_when_direction_is_airport():
    journey = _s1_journey(
        "id.AWLAAI.test", "Hamburg Airport (Flughafen)", "Hamburg Airport (Flughafen)"
    )
    result = build_positions_geojson({"journeys": [journey]}, now_ts=1010)
    assert result["features"] == []


def test_s1_non_realtime_kept_for_non_airport_direction():
    journey = _s1_journey("id.ABAPB.test", "Blankenese", "Blankenese")
    result = build_positions_geojson({"journeys": [journey]}, now_ts=1010)
    assert len(result["features"]) == 1


def test_s1_filter_does_not_apply_to_other_lines():
    # Same situation that would be dropped on S1, but on S3 - the rule only applies to S1.
    journey = {
        "journeyID": "id.test",
        "line": {
            "id": "SBH:S3_SBH_SBAHNS",
            "name": "S3",
            "direction": "Hamburg Airport (Flughafen)",
        },
        "vehicleType": "S_BAHN",
        "segments": [
            {
                "startDateTime": 1000,
                "endDateTime": 1020,
                "track": TRACK,
                "destination": "Hamburg Airport (Flughafen)",
                "startStationName": "A",
                "endStationName": "B",
            }
        ],
    }
    result = build_positions_geojson({"journeys": [journey]}, now_ts=1010)
    assert len(result["features"]) == 1


def test_delay_property_defaults_to_zero_when_absent():
    data = {"journeys": [_journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt")]}
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert feature["properties"]["delay"] == 0


def test_delay_property_reflects_realtime_delay():
    data = {
        "journeys": [_journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt", delay=3)]
    }
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert feature["properties"]["delay"] == 3


def test_text_includes_delay_line_when_positive():
    data = {
        "journeys": [
            _journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt", "DT5", delay=3)
        ]
    }
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert feature["properties"]["text"] == "Ohlstedt<br>DT5<br>+3 Minuten"


def test_text_omits_delay_line_when_zero():
    data = {
        "journeys": [
            _journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt", "DT5", delay=0)
        ]
    }
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert feature["properties"]["text"] == "Ohlstedt<br>DT5"


def test_text_omits_delay_line_when_negative():
    # Too early - only show delay above 0.
    data = {
        "journeys": [
            _journey("U_BAHN", "HHA-U:U1_HHA-U", "U1", "Ohlstedt", "DT5", delay=-2)
        ]
    }
    feature = build_positions_geojson(data, now_ts=1010)["features"][0]
    assert "Minuten" not in feature["properties"]["text"]
