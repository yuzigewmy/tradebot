from decimal import Decimal
import math

import pytest

from spot_agent.contracts import TIMEFRAMES
from spot_agent.perpetual_market import closed_windows, parse_candle
from spot_agent.perpetual_strategy import detect_setup, priced_candidate


def rows(timeframe, end):
    duration = TIMEFRAMES[timeframe]
    return [[end - (160 - i) * duration, 100, 101, 99, 100, 10] for i in range(160)]


def test_confirmation_and_aligned_windows_exclude_unclosed_and_later_context():
    end = 1794960000000
    bars = {tf: rows(tf, end) for tf in ("1m", "15m", "1h")}
    live = [str(v) for v in [end, 100, 110, 99, 108, 10, 20, 30, 0]]
    row, closed = parse_candle(live)
    assert not closed and row == [end, 100, 110, 99, 108, 20]
    bars["15m"].pop()
    assert closed_windows(bars, end) is None
    bars["15m"] = rows("15m", end) + [[end, 100, 150, 90, 140, 100]]
    assert closed_windows(bars, end)["15m"][-1][0] == end - TIMEFRAMES["15m"]
    bars["1m"].pop(-3)
    assert closed_windows(bars, end) is None


@pytest.mark.parametrize("direction", ["long", "short"])
def test_prices_geometrically_valid_and_meet_net_reward_after_costs(direction):
    t = 1794960000000
    setup = {"side": direction, "strategy": "pullback", "close": 100, "atr": 1, "stop_anchor": 99 if direction == "long" else 101}
    quote = {"bid": 99.99, "ask": 100.01, "mark": 100, "funding_rate": 0.0005 if direction == "long" else -0.0005, "funding_at": t + 1800000, "ticker_at": t, "mark_at": t}
    plan, reason = priced_candidate(setup, quote, Decimal("0.01"), t, t)
    assert reason is None and plan["funding_cost_rate"] > 0
    assert plan["net_reward_risk"] >= 2
    assert plan["stop_loss"] < plan["entry"] < plan["take_profit"] if direction == "long" else plan["take_profit"] < plan["entry"] < plan["stop_loss"]
    assert priced_candidate(setup, quote | {"ask": 100.2}, Decimal("0.01"), t, t)[0] is None


def test_breakout_uses_previous_range_and_volume_not_trigger():
    end = 1794960000000
    windows = {tf: rows(tf, end) for tf in ("1m", "15m", "1h")}
    for tf in ("15m", "1h"):
        for i, bar in enumerate(windows[tf]):
            price = 85 + i * 0.095
            bar[1:5] = [price, price + 0.5, price - 0.5, price]
    for i, bar in enumerate(windows["1m"][:-1]):
        price = 100 + (1 if i < 137 else 0.08) * math.sin(i)
        bar[1:5] = [price, price + 0.18, price - 0.18, price]
    windows["1m"][-1][1:] = [100, 100.64, 99.96, 100.60, 20]
    analysis = detect_setup(windows)
    assert analysis["setup"]["strategy"] == "breakout"
    assert analysis["setup"]["volume_ratio"] == 2
    assert analysis["setup"]["prior_high"] < 100.60
