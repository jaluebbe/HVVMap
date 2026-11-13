import json

from hvv_map import ais


def _feature(mmsi, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [9.95, 53.54]},
        "properties": {"mmsi": mmsi, "seen": "2026-11-13T17:10:07Z", **props},
    }


def test_underway_vessel_points_along_its_course():
    data = ais.build_geojson(
        [_feature(211437340, sog=3.9, cog=194.1, heading=145, nav_status=0)]
    )
    props = data["features"][0]["properties"]
    assert props["state"] == "underway"
    assert props["rotation"] == 194.1
    assert props["name"] == "TOLLERORT"


def test_moored_vessel_points_along_its_heading():
    data = ais.build_geojson(
        [_feature(211437250, sog=0, cog=158.7, heading=261, nav_status=5)]
    )
    props = data["features"][0]["properties"]
    assert props["state"] == "moored"
    assert props["rotation"] == 261


def test_slow_vessel_with_nav_status_underway_counts_as_moored():
    data = ais.build_geojson(
        [_feature(211437240, sog=0.1, heading=292, nav_status=0)]
    )
    assert data["features"][0]["properties"]["state"] == "moored"


def test_missing_angles_fall_back_and_unavailable_values_are_ignored():
    data = ais.build_geojson(
        [
            _feature(211437160, sog=0.2, cog=360, heading=511, nav_status=0),
            _feature(211437190, sog=5, heading=90, nav_status=0),
        ]
    )
    first, second = (f["properties"] for f in data["features"])
    assert first["rotation"] == 0
    assert first["cog"] is None and first["heading"] is None
    assert second["rotation"] == 90


def test_other_vessels_are_dropped():
    data = ais.build_geojson([_feature(123456789, sog=3), _feature(211437240, sog=3)])
    assert [f["properties"]["mmsi"] for f in data["features"]] == [211437240]


def test_the_fleet_fits_into_one_request():
    assert len(ais.HADAG) == 28 and len(ais.HADAG) <= 50


def test_run_once_stores_the_geojson_with_a_ttl(monkeypatch):
    stored = {}

    class _Redis:
        def set(self, key, value, ex=None):
            stored[key] = (json.loads(value), ex)

    asked = []

    def fake_fetch(token, mmsis, max_age=ais.AIS_MAX_AGE):
        asked.append((token, list(mmsis)))
        return [_feature(211437340, sog=3.9, cog=194.1, nav_status=0)]

    monkeypatch.setattr(ais, "fetch_vessels", fake_fetch)

    assert ais.run_once(_Redis(), "secret") == 1
    payload, ttl = stored[ais.REDIS_AIS_KEY]
    assert ttl == ais.AIS_TTL
    assert payload["data"]["features"][0]["properties"]["name"] == "TOLLERORT"
    assert asked == [("secret", list(ais.HADAG))]


def test_loop_without_token_returns_at_once(monkeypatch, capsys):
    monkeypatch.delenv("AIS_TOKEN", raising=False)
    ais.run_loop()
    assert "AIS_TOKEN is not set" in capsys.readouterr().out
