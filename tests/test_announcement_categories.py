from datetime import datetime, timezone

from hvv_map.announcement_categories import (
    CATEGORY_BARRIEREFREIHEIT,
    CATEGORY_SONSTIGE,
    CATEGORY_SPERRUNG,
    classify_category,
    is_accessibility_related,
    is_currently_valid,
)

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
