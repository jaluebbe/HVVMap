from hvv_map.gtfs_schedule import Schedule
from hvv_map.gtfs_tracks import build_index, fill_from_index, hop_track
from hvv_map.gti_shapes import get_segment, open_db, store_segment
from hvv_map.stations import StationInfo

LAT = 53.5
SHAPE = [(10.0 + 0.02 * i, LAT + (0.003 if 2 <= i <= 4 else 0)) for i in range(11)]


def _station(sid, lon):
    return StationInfo(sid, sid, "X", lon, LAT, sid)


STATIONS = {"A": _station("A", 10.0), "B": _station("B", 10.2)}


def _schedule(route_short_name="RE1", shape=SHAPE):
    def stop(sid, lon):
        return {"stop_id": sid, "stop_lon": str(lon), "stop_lat": str(LAT)}

    return Schedule(
        routes={
            "r": {
                "route_id": "r",
                "route_type": "2",
                "route_short_name": route_short_name,
            }
        },
        stops={"s1": stop("s1", 10.0005), "s2": stop("s2", 10.2)},
        trips={"t": {"trip_id": "t", "route_id": "r", "shape_id": "sh"}},
        trip_stop_times={"t": [(1, "s1", 0, 0), (2, "s2", 600, 600)]},
        calendar_rows=[],
        calendar_dates={},
        shapes={"sh": shape},
    )


def _service(short_info="RE", ids=("A", "B")):
    return {
        "line": {"id": "x", "name": "RE1", "type": {"shortInfo": short_info}},
        "subline": [{"id": i, "name": i} for i in ids],
    }


def test_hop_track_cuts_the_shape_at_the_stations():
    index = build_index(_schedule())

    track = hop_track(index, "R", (10.0, LAT), (10.2, LAT))

    assert track[0] == [10.0, LAT] or track[0] == (10.0, LAT)
    assert len(track) == len(SHAPE)


def test_hop_track_needs_a_shape_of_the_same_mode():
    index = build_index(_schedule())

    assert hop_track(index, "S", (10.0, LAT), (10.2, LAT)) is None


def test_straight_hop_is_filled_from_the_gtfs_shape():
    conn = open_db(":memory:")
    store_segment(conn, "A", "B", [[10.0, LAT], [10.2, LAT]])

    filled = fill_from_index(
        conn, {"1": _service()}, STATIONS, build_index(_schedule())
    )

    assert filled == 1
    assert len(get_segment(conn, "A", "B")) == len(SHAPE)


def test_real_gti_track_is_left_alone():
    conn = open_db(":memory:")
    real = [[10.0 + 0.01 * i, LAT] for i in range(21)]
    store_segment(conn, "A", "B", real)

    filled = fill_from_index(
        conn, {"1": _service()}, STATIONS, build_index(_schedule())
    )

    assert filled == 0
    assert get_segment(conn, "A", "B") == real


def test_replacement_buses_never_take_a_rail_shape():
    conn = open_db(":memory:")
    store_segment(conn, "A", "B", [[10.0, LAT], [10.2, LAT]])

    filled = fill_from_index(
        conn, {"1": _service("Bus")}, STATIONS, build_index(_schedule())
    )

    assert filled == 0


def test_sparse_gtfs_shape_does_not_replace_the_straight_link():
    conn = open_db(":memory:")
    store_segment(conn, "A", "B", [[10.0, LAT], [10.2, LAT]])
    sparse = [(10.0, LAT), (10.1, LAT + 0.01), (10.2, LAT)]

    filled = fill_from_index(
        conn, {"1": _service()}, STATIONS, build_index(_schedule(shape=sparse))
    )

    assert filled == 0


def test_without_a_feed_nothing_is_filled(tmp_path):
    from hvv_map.gtfs_tracks import fill_from_gtfs

    assert fill_from_gtfs(open_db(":memory:"), {}, {}, str(tmp_path)) == 0
