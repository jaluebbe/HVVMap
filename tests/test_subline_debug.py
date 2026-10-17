from hvv_map.gti_shapes import open_db, store_segment
from hvv_map.subline_debug import describe_subline


def test_describe_subline_reports_real_straight_missing_and_skipping_hops():
    conn = open_db(":memory:")
    store_segment(conn, "A", "B", [[0, 0], [1, 1], [2, 2]])
    store_segment(conn, "B", "D", [[2, 2], [3, 3]])
    subline = [{"id": i, "name": i.lower()} for i in "ABCDE"]

    assert describe_subline(subline, conn) == [
        "  a -> b: 3 Punkte",
        "  b -> d [überspringt 1]: 2 Punkte (Luftlinie)",
        "  d -> e: fehlt",
    ]
