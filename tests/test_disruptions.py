from datetime import datetime, timezone

from hvv_map.announcement_categories import CATEGORY_SPERRUNG
from hvv_map.disruptions import build_disruptions_geojson
from hvv_map.stations import StationInfo

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

STATIONS = {
    "Master:1": StationInfo(
        id="Master:1", name="Lattenkamp", city="Hamburg", lon=10.0, lat=53.6,
        combined_name="Lattenkamp",
    ),  # fmt: skip
    "Master:2": StationInfo(
        id="Master:2", name="Fuhlsbüttel Nord", city="Hamburg", lon=10.01, lat=53.65,
        combined_name="Fuhlsbüttel Nord",
    ),  # fmt: skip
    "SCM:9057819": StationInfo(
        id="SCM:9057819", name="Hbf", city="Lübeck", lon=10.67, lat=53.87,
        combined_name="Lübeck Hbf",
    ),  # fmt: skip
}


def _announcement(summary, description, begin_id, end_id, line_name="U1", links=None):
    return {
        "summary": summary,
        "description": description,
        "links": links or [],
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


def test_build_disruptions_geojson_labels_with_combined_name():
    # Regression: listStations splits some stations into name="Hbf",
    # city="Lübeck" - the popup heading and station_name property must use
    # the recombined combined_name ("Lübeck Hbf"), not the bare name.
    data = {
        "announcements": [
            _announcement("Verspätungen", "", "SCM:9057819", "SCM:9057819"),
        ]
    }
    feature = build_disruptions_geojson(data, STATIONS, now=NOW)["features"][0]
    assert feature["properties"]["station_name"] == "Lübeck Hbf"
    assert feature["properties"]["text"].startswith("Lübeck Hbf:")


def test_build_disruptions_geojson_strips_redundant_prefix_using_combined_name():
    # A summary already prefixed with the FULL combined name (as real
    # announcement text does) must not be duplicated - stripping against
    # the bare "Hbf" alone wouldn't match this prefix at all.
    data = {
        "announcements": [
            _announcement(
                "Lübeck Hbf: Verspätungen im Zugverkehr",
                "",
                "SCM:9057819",
                "SCM:9057819",
            )
        ]
    }
    feature = build_disruptions_geojson(data, STATIONS, now=NOW)["features"][0]
    assert feature["properties"]["text"] == "Lübeck Hbf:<br>Verspätungen im Zugverkehr"


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


def test_build_disruptions_geojson_messages_include_description_and_links():
    links = [{"label": "Störungskarte", "url": "https://example.com/karte.png"}]
    data = {
        "announcements": [
            _announcement(
                "U1-Sperrung",
                "Volltext der Meldung",
                "Master:1",
                "Master:1",
                links=links,
            )
        ]
    }
    feature = build_disruptions_geojson(data, STATIONS, now=NOW)["features"][0]
    messages = feature["properties"]["messages"]
    assert len(messages) == 1
    assert messages[0]["summary"] == "U1-Sperrung"
    assert messages[0]["description"] == "Volltext der Meldung"
    assert messages[0]["links"] == links


def test_build_disruptions_geojson_messages_one_entry_per_distinct_announcement():
    data = {
        "announcements": [
            _announcement("Meldung A", "Text A", "Master:1", "Master:1"),
            _announcement("Meldung B", "Text B", "Master:1", "Master:1"),
        ]
    }
    feature = build_disruptions_geojson(data, STATIONS, now=NOW)["features"][0]
    messages = feature["properties"]["messages"]
    assert [m["summary"] for m in messages] == ["Meldung A", "Meldung B"]
    assert [m["description"] for m in messages] == ["Text A", "Text B"]
