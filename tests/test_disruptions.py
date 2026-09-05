from datetime import datetime, timezone

from hvv_map.disruptions import (
    CATEGORY_BARRIEREFREIHEIT,
    CATEGORY_SONSTIGE,
    CATEGORY_SPERRUNG,
    build_disruptions_geojson,
    classify_category,
    is_accessibility_related,
    is_currently_valid,
)
from hvv_map.stations import StationInfo

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def test_is_accessibility_related_matches_rollstuhl_variants():
    for text in [
        "Suchoption Rollstuhl / Kinderwagen",
        'Suchoption "Rollstuhl/Kinderwagen"',
        "Suchoption „Rollstuhl/Kinderwagen“",
    ]:
        assert is_accessibility_related({"description": text})


def test_is_accessibility_related_matches_elevator_out_of_service():
    text = "der Aufzug zum Bahnsteig ist außer Betrieb"
    assert is_accessibility_related({"description": text})


def test_is_accessibility_related_false_for_unrelated_text():
    assert not is_accessibility_related({"description": "Gleisbauarbeiten"})


def test_classify_category_accessibility_wins_over_title_keywords():
    announcement = {"summary": "Sperrung", "description": "Rollstuhl / Kinderwagen"}
    assert classify_category(announcement) == CATEGORY_BARRIEREFREIHEIT


def test_classify_category_sperrung_from_title():
    assert (
        classify_category({"summary": "U1-Sperrung", "description": ""})
        == CATEGORY_SPERRUNG
    )
    assert (
        classify_category({"summary": "Ersatzverkehr mit Bussen", "description": ""})
        == CATEGORY_SPERRUNG
    )


def test_classify_category_defaults_to_sonstige():
    assert (
        classify_category({"summary": "Fahrplanänderung", "description": ""})
        == CATEGORY_SONSTIGE
    )


def test_is_currently_valid_true_when_no_validities():
    assert is_currently_valid({}, now=NOW)


def test_is_currently_valid_inside_window():
    announcement = {
        "validities": [
            {"begin": "2026-09-01T00:00:00+0200", "end": "2026-09-03T00:00:00+0200"}
        ]
    }
    assert is_currently_valid(announcement, now=NOW)


def test_is_currently_valid_outside_window():
    announcement = {
        "validities": [
            {"begin": "2026-10-01T00:00:00+0200", "end": "2026-10-03T00:00:00+0200"}
        ]
    }
    assert not is_currently_valid(announcement, now=NOW)


STATIONS = {
    "Master:1": StationInfo(
        id="Master:1", name="Lattenkamp", city="Hamburg", lon=10.0, lat=53.6
    ),
    "Master:2": StationInfo(
        id="Master:2", name="Fuhlsbüttel Nord", city="Hamburg", lon=10.01, lat=53.65
    ),
}


def _announcement(summary, description, begin_id, end_id, line_name="U1"):
    return {
        "summary": summary,
        "description": description,
        "locations": [
            {
                "line": {"name": line_name},
                "begin": {"id": begin_id, "name": "x"},
                "end": {"id": end_id, "name": "y"},
            }
        ],
        "validities": [
            {"begin": "2026-09-01T00:00:00+0200", "end": "2026-09-03T00:00:00+0200"}
        ],
    }


def test_build_disruptions_geojson_creates_one_feature_per_station():
    data = {
        "announcements": [
            _announcement("U1-Sperrung", "", "Master:1", "Master:2"),
        ]
    }
    result = build_disruptions_geojson(data, STATIONS, now=NOW)
    assert len(result["features"]) == 2
    station_names = {f["properties"]["station_name"] for f in result["features"]}
    assert station_names == {"Lattenkamp", "Fuhlsbüttel Nord"}


def test_build_disruptions_geojson_sets_category_and_color():
    data = {"announcements": [_announcement("U1-Sperrung", "", "Master:1", "Master:1")]}
    feature = build_disruptions_geojson(data, STATIONS, now=NOW)["features"][0]
    assert feature["properties"]["category"] == CATEGORY_SPERRUNG
    assert feature["properties"]["color"] == "#ff6600"


def test_build_disruptions_geojson_sets_mode_from_line():
    data = {
        "announcements": [
            _announcement("U1-Sperrung", "", "Master:1", "Master:1", "U1")
        ]
    }
    feature = build_disruptions_geojson(data, STATIONS, now=NOW)["features"][0]
    assert feature["properties"]["modes"] == ["U"]


def test_build_disruptions_geojson_strips_redundant_station_prefix():
    data = {
        "announcements": [
            _announcement(
                "Aufzug kaputt",
                "Lattenkamp: Der Aufzug ist außer Betrieb",
                "Master:1",
                "Master:1",
            )
        ]
    }
    feature = build_disruptions_geojson(data, STATIONS, now=NOW)["features"][0]
    assert "Lattenkamp: Lattenkamp" not in feature["properties"]["text"]


def test_build_disruptions_geojson_skips_expired_announcements():
    announcement = _announcement("U1-Sperrung", "", "Master:1", "Master:1")
    announcement["validities"] = [
        {"begin": "2020-01-01T00:00:00+0200", "end": "2020-01-02T00:00:00+0200"}
    ]
    result = build_disruptions_geojson(
        {"announcements": [announcement]}, STATIONS, now=NOW
    )
    assert result["features"] == []


def test_build_disruptions_geojson_skips_unknown_station_ids():
    data = {
        "announcements": [
            _announcement("U1-Sperrung", "", "Master:unknown", "Master:unknown")
        ]
    }
    result = build_disruptions_geojson(data, STATIONS, now=NOW)
    assert result["features"] == []
