from datetime import datetime, timezone

from hvv_map import aisstream

RECEIVED = datetime(2026, 11, 20, 16, 15, tzinfo=timezone.utc)


def _report(mmsi=211437340, **report):
    return {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": mmsi, "time_utc": "2026-11-20 16:12:03.123456 +0000 UTC"},
        "Message": {
            "PositionReport": {
                "UserID": mmsi,
                "Latitude": 53.544,
                "Longitude": 9.966,
                "Sog": 7.2,
                "Cog": 265.0,
                "TrueHeading": 263,
                "NavigationalStatus": 0,
                **report,
            }
        },
    }


def test_subscription_filters_the_fleet_as_strings():
    sub = aisstream.subscription("key")
    assert sub["APIKey"] == "key"
    assert "211437340" in sub["FiltersShipMMSI"]
    assert len(sub["FiltersShipMMSI"]) == len(aisstream.HADAG) <= 200


def test_position_report_becomes_a_feature_with_its_own_time():
    f = aisstream.to_feature(_report(), RECEIVED)
    assert f["geometry"]["coordinates"] == [9.966, 53.544]
    p = f["properties"]
    assert (p["mmsi"], p["sog"], p["cog"], p["heading"]) == (211437340, 7.2, 265.0, 263)
    assert p["seen"] == "2026-11-20T16:12:03Z"


def test_missing_time_falls_back_to_receipt():
    message = _report()
    del message["MetaData"]["time_utc"]
    assert (
        aisstream.to_feature(message, RECEIVED)["properties"]["seen"]
        == "2026-11-20T16:15:00Z"
    )


def test_other_messages_and_invalid_positions_are_skipped():
    assert aisstream.to_feature({"MessageType": "ShipStaticData"}, RECEIVED) is None
    assert aisstream.to_feature(_report(Latitude=91), RECEIVED) is None


def test_old_positions_are_dropped_and_names_added():
    fresh = aisstream.to_feature(_report(), RECEIVED)
    old = aisstream.to_feature(_report(211437250), RECEIVED)
    old["properties"]["seen"] = "2026-11-20T15:00:00Z"
    data = aisstream.current({1: fresh, 2: old}, RECEIVED)
    assert [f["properties"]["name"] for f in data["features"]] == ["TOLLERORT"]
    assert data["features"][0]["properties"]["state"] == "underway"
