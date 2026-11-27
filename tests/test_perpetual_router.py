from decimal import Decimal
import math

from spot_agent.binance_market import BinanceRules
from spot_agent.binance_monitor import ALLOWED_STRATEGIES, STRATEGIES
from spot_agent.contracts import TIMEFRAMES
from spot_agent.perpetual_dual import DUAL_STRATEGY
from spot_agent.perpetual_macro import MACRO_A1_AGGRESSIVE
from spot_agent.perpetual_router import (
    AUTO_REGIME_STRATEGY, auto_regime_setup, auto_regime_signal_candidate, detect_regime,
)


def _trend_bars(timeframe, count, end, *, start_price=100.0, step=0.5, spread=0.6):
    dur = TIMEFRAMES[timeframe]
    rows = []
    for i in range(count):
        ts = end - (count - i) * dur
        p = start_price + i * step
        rows.append([ts, p - 0.1, p + spread, p - spread, p, 100.0])
    return rows


def _chop_bars(timeframe, count, end, *, base=100.0, amp=1.2):
    dur = TIMEFRAMES[timeframe]
    rows = []
    for i in range(count):
        ts = end - (count - i) * dur
        p = base + amp * math.sin(i * 0.9)
        rows.append([ts, p - 0.1, p + 0.6, p - 0.6, p, 100.0])
    return rows


def test_default_live_strategy_is_auto_regime_router():
    assert STRATEGIES == {
        "BTC-USDT-SWAP": AUTO_REGIME_STRATEGY,
        "ETH-USDT-SWAP": AUTO_REGIME_STRATEGY,
        "SOL-USDT-SWAP": AUTO_REGIME_STRATEGY,
    }
    for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"):
        assert AUTO_REGIME_STRATEGY in ALLOWED_STRATEGIES[inst]
        assert DUAL_STRATEGY in ALLOWED_STRATEGIES[inst]


def test_auto_regime_routes_strong_trend_to_s10_8pct_30x():
    end = 1795500000000 // TIMEFRAMES["1d"] * TIMEFRAMES["1d"]
    d1 = _trend_bars("1d", 160, end, start_price=100.0, step=1.0, spread=0.5)
    h4 = _trend_bars("4h", 160, end, start_price=180.0, step=0.6, spread=0.4)
    h1 = _trend_bars("1h", 160, end, start_price=220.0, step=0.25, spread=0.5)
    # Trigger S4 pullback on latest 1H close
    peak = h1[-5][4]
    h1[-4][1:5] = [peak, peak + 0.2, peak - 6.0, peak - 5.5]
    h1[-3][1:5] = [peak - 5.5, peak - 5.0, peak - 11.0, peak - 10.5]
    h1[-2][1:5] = [peak - 10.5, peak - 9.0, peak - 14.0, peak - 13.0]
    h1[-1][1:6] = [peak - 13.0, peak + 5.0, peak - 13.2, peak + 4.0, 120.0]

    setup = auto_regime_setup({"1d": d1, "4h": h4, "1h": h1})
    assert setup["regime"]["state"] == "trend"
    assert setup["routed_strategy"] == DUAL_STRATEGY
    assert setup["strategy"] == AUTO_REGIME_STRATEGY
    assert setup["engine"] == "s4_pullback"

    rules = BinanceRules(Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("10"), Decimal("20"))
    now = end + TIMEFRAMES["1m"] + 1000
    px = setup["close"]
    quote = {"bid": px - 0.05, "ask": px + 0.05, "last": px, "mark": px,
             "bid_size": 10.0, "ask_size": 10.0, "ticker_at": now, "funding_at": now + 60_000}
    cand, issue = auto_regime_signal_candidate("ETH-USDT-SWAP", setup, quote, rules, now, binance=True)
    assert issue is None
    assert cand["strategy"] == AUTO_REGIME_STRATEGY
    assert cand["routed_strategy"] == DUAL_STRATEGY
    assert cand["leverage"] == 30
    assert cand["risk_percent"] == 8.0
    assert cand["max_notional_multiple"] == 15.0
    assert abs(cand["entry"] - cand["stop_loss"]) / cand["entry"] <= 0.0245


def test_auto_regime_routes_chop_or_divergence_to_a1_aggressive_15x():
    end = 1795500000000 // TIMEFRAMES["1d"] * TIMEFRAMES["1d"] + 4 * TIMEFRAMES["1h"]
    # 1D bullish, but 4H had a bearish pullback (misaligned -> chop/mixed regime!) before Supertrend flip
    d1 = _trend_bars("1d", 160, end // TIMEFRAMES["1d"] * TIMEFRAMES["1d"], start_price=100.0, step=0.5)
    h4 = _trend_bars("4h", 160, end, start_price=180.0, step=-0.2, spread=0.4)
    base = h4[-2][4]
    h4[-1][1:6] = [base, base + 12.0, base - 0.2, base + 11.0, 200.0]
    h1 = _chop_bars("1h", 160, end, base=160.0)

    regime = detect_regime({"1d": d1, "4h": h4, "1h": h1})
    assert regime["state"] == "chop"
    assert regime["routed_strategy"] == MACRO_A1_AGGRESSIVE
    assert regime["active_leverage"] == 15

    setup = auto_regime_setup({"1d": d1, "4h": h4, "1h": h1})
    assert setup["strategy"] == AUTO_REGIME_STRATEGY
    assert setup["routed_strategy"] == MACRO_A1_AGGRESSIVE
    assert setup["engine"] == "m3_4h_supertrend"

    rules = BinanceRules(Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("10"), Decimal("20"))
    now = end + TIMEFRAMES["1m"] + 1000
    px = setup["close"]
    quote = {"bid": px - 0.05, "ask": px + 0.05, "last": px, "mark": px,
             "bid_size": 10.0, "ask_size": 10.0, "ticker_at": now, "funding_at": now + 60_000}
    cand, issue = auto_regime_signal_candidate("ETH-USDT-SWAP", setup, quote, rules, now, binance=True)
    assert issue is None
    assert cand["strategy"] == AUTO_REGIME_STRATEGY
    assert cand["routed_strategy"] == MACRO_A1_AGGRESSIVE
    assert cand["leverage"] == 15
    assert cand["risk_percent"] == 8.0
    assert cand["max_notional_multiple"] == 10.0
    assert abs(cand["entry"] - cand["stop_loss"]) / cand["entry"] <= 0.0505
