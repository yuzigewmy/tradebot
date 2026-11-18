import numpy as np
import pytest
import talib

from spot_agent.market import build_frame
from spot_agent.technicals import analyze_frame, confirmed_swings


def fixture_rows(count=620):
    close = 100 + np.arange(count) * 0.08 + np.sin(np.arange(count) / 7)
    return [[i * 3600000, float(c - 0.1), float(c + 0.8), float(c - 0.9), float(c), 10 + i % 8] for i, c in enumerate(close)]


def test_indicators_match_talib_and_volume_baseline_excludes_current():
    rows = fixture_rows()
    frame = build_frame(rows, "test", "BTC/USDT", "1h", "synthetic", len(rows) * 3600000)
    result = analyze_frame(frame)
    close = np.array([r[4] for r in rows])
    assert result["indicators"]["rsi14"] == pytest.approx(talib.RSI(close, timeperiod=14)[-1])
    assert result["indicators"]["ema200"] == pytest.approx(talib.EMA(close, timeperiod=200)[-1])
    assert result["indicators"]["volume_ratio"] == pytest.approx(rows[-1][5] / np.mean([r[5] for r in rows[-21:-1]]))


def test_swings_are_only_known_after_confirmation():
    rows = fixture_rows(90)
    prefix = confirmed_swings(rows[:50], 3600000)
    full = confirmed_swings(rows, 3600000)
    assert prefix == [s for s in full if s["confirmed_at"] <= 50 * 3600000]
    assert all(s["confirmed_at"] == s["occurred_at"] + 3 * 3600000 for s in full)


def test_gap_uses_only_contiguous_tail_and_disables_warmup():
    rows = fixture_rows(620)
    del rows[590]
    frame = build_frame(rows, "test", "BTC/USDT", "1h", "synthetic", 620 * 3600000)
    result = analyze_frame(frame)
    assert result["indicators"]["ema200"] is None
    assert result["ready"] is False


def test_equal_confirmed_highs_and_lows_are_not_lower_structure():
    prices = [100, 101, 105, 101, 100, 95, 100, 101, 105, 101, 100, 95, 100, 101]
    rows = [[i * 3600000, c, c + 0.1, c - 0.1, c, 10] for i, c in enumerate(prices)]
    frame = build_frame(rows, "test", "BTC/USDT", "1h", "synthetic", len(rows) * 3600000)
    assert analyze_frame(frame)["structure"] == "EH/EL"
