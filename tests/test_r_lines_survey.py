from hvv_map.r_lines_survey import (
    format_summary,
    resolve_station,
    summarize,
    survey,
)
from hvv_map.stations import StationInfo


def _station(name: str, combined: str = "") -> StationInfo:
    return StationInfo("id:" + name, name, "X", 10.0, 53.0, combined)


def _dep(line: str, direction: str, service_id: int) -> dict:
    return {
        "serviceId": service_id,
        "line": {"name": line, "direction": direction},
    }


def test_resolve_prefers_exact_name_and_hints_otherwise():
    catalog = [_station("Harburg", "Hamburg-Harburg"), _station("Harburg Rathaus")]
    assert resolve_station(catalog, "harburg") == ("Harburg", [])
    assert resolve_station(catalog, "Hamburg-Harburg") == ("Harburg", [])
    name, hints = resolve_station(catalog, "Rathaus")
    assert name is None and hints == ["Harburg Rathaus"]


def test_summarize_counts_distinct_trips_per_line_and_marks_known_lines():
    responses = {
        "A": {"departures": [_dep("RE6", "Westerland", 1), _dep("RE6", "Hamburg", 2)]},
        "B": {"departures": [_dep("RE6", "Westerland", 1), _dep("RE3", "Uelzen", 3)]},
    }
    summary = summarize(responses)
    assert summary["RE6"]["trips"] == 2
    assert summary["RE6"]["stations"] == {"A": 2, "B": 1}
    assert summary["RE6"]["directions"] == ["Hamburg", "Westerland"]
    assert summary["RE3"]["wanted"] is False


def test_known_line_and_its_replacement_bus_are_marked_wanted():
    summary = summarize(
        {"A": {"departures": [_dep("RB61", "Itzehoe", 1), _dep("RB61-SEV", "x", 2)]}}
    )
    assert summary["RB61"]["wanted"] and summary["RB61-SEV"]["wanted"]


def test_format_lists_errors():
    text = format_summary({}, {"Nirgendwo": "ERROR_TEXT"})
    assert "Fehler Nirgendwo: ERROR_TEXT" in text


class _FakeClient:
    def send(self, endpoint, request):
        name = request["stations"][0]["name"]
        if name == "Kaputt":
            return {"returnCode": "ERROR_TEXT", "errorText": "unknown station"}
        if name == "Wirft":
            raise RuntimeError("boom")
        return {"returnCode": "OK", "departures": [_dep("RE7", "Kiel", 9)]}


def test_survey_keeps_going_after_a_failing_station(monkeypatch):
    monkeypatch.setattr("hvv_map.r_lines_survey.REQUEST_PAUSE", 0)
    responses, errors = survey(_FakeClient(), ["Kaputt", "Wirft", "Elmshorn"])
    assert list(responses) == ["Elmshorn"]
    assert errors["Kaputt"] == "unknown station" and "boom" in errors["Wirft"]
