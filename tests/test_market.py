import pytest

from spot_agent.market import build_frame, demo_market


def test_closed_candles_and_gap_are_explicit():
    rows = [[t, 10, 12, 9, 11, 5] for t in [0, 3600000, 10800000, 14400000]]
    frame = build_frame(rows, "demo", "BTC/USDT", "1h", "synthetic", 14401000)
    assert [r[0] for r in frame.rows] == [0, 3600000, 10800000]
    assert frame.quality["gap_count"] == 1
    assert frame.quality["contiguous_count"] == 1
    assert frame.quality["excluded_open"] == 1


def test_ohlcv_invalid_range_is_rejected_at_data_boundary():
    with pytest.raises(ValueError, match="OHLC"):
        build_frame([[0, 10, 9, 8, 11, 1]], "okx", "BTC/USDT", "1h", "live", 7200000)


def test_demo_uses_same_underlying_hourly_market():
    frames = demo_market("BTC/USDT", 1794960000000)
    hourly = {r[0]: r for r in frames["1h"].rows}
    four = frames["4h"].rows[-1]
    members = [hourly[four[0] + i * 3600000] for i in range(4)]
    assert four[1] == members[0][1]
    assert four[4] == members[-1][4]
    assert four[2] == max(r[2] for r in members)
    assert four[5] == pytest.approx(sum(r[5] for r in members))
