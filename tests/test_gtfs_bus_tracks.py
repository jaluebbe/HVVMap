import json

from hvv_map.gtfs_bus_tracks import (
    build_bus_tracks_geojson,
    is_bus_route_type,
    simplify,
)


def _write(path, header, rows):
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def _feed(tmp_path, shapes):
    _write(
        tmp_path / "routes.txt",
        "route_id,route_short_name,route_type",
        ["b1,150,3", "b2,X22,700", "r1,RE1,2", "u1,U1,402"],
    )
    _write(
        tmp_path / "trips.txt",
        "route_id,service_id,trip_id,shape_id",
        ["b1,s,t1,sh1", "b1,s,t2,sh2", "b2,s,t3,sh3", "r1,s,t4,rail", "u1,s,t5,u"],
    )
    _write(
        tmp_path / "shapes.txt",
        "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence",
        shapes,
    )
    return str(tmp_path)


def _line(shape_id, lon0, count=5):
    return [f"{shape_id},53.5,{lon0 + 0.001 * i:.6f},{i}" for i in range(count)]


def test_bus_route_types():
    assert is_bus_route_type("3")
    assert is_bus_route_type("714")
    assert not is_bus_route_type("2")
    assert not is_bus_route_type("402")
    assert not is_bus_route_type("")


def test_simplify_keeps_corners_and_drops_collinear_points():
    straight = [(10.0 + 0.001 * i, 53.5) for i in range(6)]
    corner = [(10.0, 53.5), (10.005, 53.5), (10.005, 53.505)]

    assert simplify(straight) == [straight[0], straight[-1]]
    assert simplify(corner) == corner


def test_only_bus_shapes_are_drawn(tmp_path):
    shapes = _line("sh1", 10.0) + _line("rail", 11.0) + _line("u", 12.0)
    feed = _feed(tmp_path, shapes)

    result = build_bus_tracks_geojson(feed)

    assert len(result["features"]) == 1
    assert result["features"][0]["properties"]["lines"] == ["150"]


def test_identical_tracks_are_merged_with_all_their_lines(tmp_path):
    shapes = _line("sh1", 10.0) + _line("sh3", 10.0)
    feed = _feed(tmp_path, shapes)

    [feature] = build_bus_tracks_geojson(feed)["features"]

    assert feature["properties"]["lines"] == ["150", "X22"]
    assert feature["properties"]["search"] == "150 x22"


def test_points_are_ordered_by_sequence_and_simplified(tmp_path):
    rows = _line("sh1", 10.0)
    feed = _feed(tmp_path, list(reversed(rows)))

    [feature] = build_bus_tracks_geojson(feed)["features"]

    assert feature["geometry"]["coordinates"] == [[10.0, 53.5], [10.004, 53.5]]


def test_fetcher_stores_the_tracks_once_per_feed(tmp_path, monkeypatch):
    from hvv_map import gtfs_fetcher as module

    class FakeRedis:
        def __init__(self):
            self.values = {}
            self.sets = 0

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value):
            self.values[key] = value
            self.sets += 1

    feed = _feed(tmp_path, _line("sh1", 10.0))
    monkeypatch.setattr(module, "GTFS_DIR", feed)
    redis_client = FakeRedis()

    module._build_bus_tracks(redis_client, 1.0)
    module._build_bus_tracks(redis_client, 1.0)
    module._build_bus_tracks(redis_client, 2.0)

    stored = json.loads(redis_client.values[module.BUS_TRACKS_KEY])
    assert redis_client.sets == 2  # rebuilt only for the changed feed
    assert stored["feed_mtime"] == 2.0
    assert len(stored["data"]["features"]) == 1
