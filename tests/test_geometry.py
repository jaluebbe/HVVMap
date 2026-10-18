import pytest

from hvv_map.geometry import cumulative_distances, haversine, point_at_distance

LINE = [(10.0, 53.5), (10.001, 53.5), (10.002, 53.5)]


def test_haversine_of_one_millidegree_of_latitude_is_about_111_m():
    assert haversine(10.0, 53.5, 10.0, 53.501) == pytest.approx(111.2, abs=0.5)


def test_cumulative_distances_start_at_zero_and_grow():
    dists = cumulative_distances(LINE)
    assert dists[0] == 0.0
    assert dists[2] == pytest.approx(2 * dists[1])


def test_point_at_half_the_length_is_the_middle_point():
    dists = cumulative_distances(LINE)
    lon, lat = point_at_distance(LINE, dists, dists[-1] / 2)
    assert lon == pytest.approx(10.001)
    assert lat == pytest.approx(53.5)


def test_point_at_distance_is_clamped_to_the_ends():
    dists = cumulative_distances(LINE)
    assert point_at_distance(LINE, dists, -5) == LINE[0]
    assert point_at_distance(LINE, dists, dists[-1] + 5) == LINE[-1]
