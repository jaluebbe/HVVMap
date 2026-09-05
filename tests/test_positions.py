from hvv_map.positions import (
    interpolate_journey_position_detailed,
    interpolate_position,
)

SEGMENT = {
    "startDateTime": 1000,
    "endDateTime": 1020,
    "track": {
        "track": [10.0, 53.5, 10.001, 53.5, 10.002, 53.5],  # 3 points, straight line
        "coordinateType": "EPSG_4326",
    },
}


def test_progress_zero_at_segment_start():
    progress, position = interpolate_position(SEGMENT, now_ts=1000)
    assert progress == 0.0
    assert position == (10.0, 53.5)


def test_progress_one_at_segment_end():
    progress, position = interpolate_position(SEGMENT, now_ts=1020)
    assert progress == 1.0
    assert position == (10.002, 53.5)


def test_progress_clamped_before_start():
    progress, _ = interpolate_position(SEGMENT, now_ts=500)
    assert progress == 0.0


def test_progress_clamped_after_end():
    progress, _ = interpolate_position(SEGMENT, now_ts=5000)
    assert progress == 1.0


def test_progress_halfway_lands_near_middle_point():
    progress, position = interpolate_position(SEGMENT, now_ts=1010)
    assert progress == 0.5
    lon, lat = position
    assert abs(lon - 10.001) < 0.0001
    assert abs(lat - 53.5) < 0.0001


def test_zero_length_segment_returns_full_progress():
    zero_length = {**SEGMENT, "endDateTime": 1000}
    progress, position = interpolate_position(zero_length, now_ts=1000)
    assert progress == 1.0
    assert position == (10.002, 53.5)


def test_missing_track_returns_none():
    segment = {"startDateTime": 1000, "endDateTime": 1020}
    assert interpolate_position(segment, now_ts=1010) is None


def test_track_with_single_point_returns_none():
    segment = {
        **SEGMENT,
        "track": {"track": [10.0, 53.5], "coordinateType": "EPSG_4326"},
    }
    assert interpolate_position(segment, now_ts=1010) is None


def test_journey_selects_segment_covering_now():
    journey = {
        "segments": [
            {**SEGMENT, "startDateTime": 1000, "endDateTime": 1020},
            {**SEGMENT, "startDateTime": 1020, "endDateTime": 1040},
        ]
    }
    progress, _, _ = interpolate_journey_position_detailed(journey, now_ts=1030)
    assert progress == 0.5  # halfway through the SECOND segment


def test_detailed_returns_progress_position_and_segment():
    journey = {"segments": [SEGMENT]}
    result = interpolate_journey_position_detailed(journey, now_ts=1010)
    assert result is not None
    progress, position, segment = result
    assert progress == 0.5
    assert segment is SEGMENT


def test_detailed_returns_none_when_basic_version_would():
    journey = {"segments": []}
    assert interpolate_journey_position_detailed(journey, now_ts=1010) is None
