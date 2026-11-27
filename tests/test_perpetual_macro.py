from decimal import Decimal

from spot_agent.binance_market import BinanceRules
from spot_agent.binance_monitor import ALLOWED_STRATEGIES, STRATEGIES
from spot_agent.contracts import TIMEFRAMES
from spot_agent.perpetual_dual import DUAL_STRATEGY
from spot_agent.perpetual_macro import (
    MACRO_A1_AGGRESSIVE, MACRO_A1_STEADY, MACRO_PROFILES,
    macro_a1_setup, macro_a1_signal_candidate,
)


def _bars(timeframe, count, end, *, start_price=100.0, step=0.5, spread=0.6):
    dur = TIMEFRAMES[timeframe]
    rows = []
    for i in range(count):
        ts = end - (count - i) * dur
        p = start_price + i * step
        rows.append([ts, p - 0.1, p + spread, p - spread, p, 100.0])
    return rows


def test_default_live_strategy_remains_s10_while_a1_variants_are_selectable():
    assert STRATEGIES == {"BTC-USDT-SWAP": DUAL_STRATEGY, "ETH-USDT-SWAP": DUAL_STRATEGY}
    for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
        assert MACRO_A1_AGGRESSIVE in ALLOWED_STRATEGIES[inst]
        assert MACRO_A1_STEADY in ALLOWED_STRATEGIES[inst]


def test_macro_a1_triggers_4h_supertrend_flip_and_enforces_profile_stop_caps():
    end = 1795500000000 // TIMEFRAMES["1d"] * TIMEFRAMES["1d"] + 4 * TIMEFRAMES["1h"]
    d1 = _bars("1d", 160, end // TIMEFRAMES["1d"] * TIMEFRAMES["1d"], start_price=100.0, step=0.5)
    h4 = _bars("4h", 160, end, start_price=180.0, step=-0.2, spread=0.4)
    # Drive Supertrend bearish for recent bars, then surge sharply upward on latest 4H close
    base = h4[-2][4]
    h4[-1][1:6] = [base, base + 12.0, base - 0.2, base + 11.0, 200.0]

    for strategy in (MACRO_A1_AGGRESSIVE, MACRO_A1_STEADY):
        setup = macro_a1_setup({"4h": h4, "1d": d1}, strategy)
        assert setup["strategy"] == strategy
        assert setup["engine"] == "m3_4h_supertrend"
        assert setup["side"] == "long"
        assert setup["timeframe"] == "4h"
        assert setup["breakeven_r"] == 1.5
        assert setup["trailing_atr"] == 3.0

        rules = BinanceRules(Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("10"), Decimal("20"))
        now = end + TIMEFRAMES["1m"] + 1000
        px = setup["close"]
        quote = {"bid": px - 0.05, "ask": px + 0.05, "last": px, "mark": px,
                 "bid_size": 10.0, "ask_size": 10.0, "ticker_at": now, "funding_at": now + 60_000}
        cand, issue = macro_a1_signal_candidate("ETH-USDT-SWAP", strategy, setup, quote, rules, now, binance=True)
        assert issue is None
        profile = MACRO_PROFILES[strategy]
        assert cand["leverage"] == int(profile.leverage)
        assert cand["risk_percent"] == float(profile.risk_percent)
        assert cand["max_notional_multiple"] == float(profile.max_notional_multiple)
        assert abs(cand["entry"] - cand["stop_loss"]) / cand["entry"] <= profile.max_stop_pct + 0.0005


def test_macro_a1_triggers_1d_donchian_breakout_on_daily_close():
    end = 1795500000000 // TIMEFRAMES["1d"] * TIMEFRAMES["1d"]
    # Build steady uptrend daily bars, flatten last 15 bars, then break above prior 14-day high on final daily close
    d1 = _bars("1d", 160, end, start_price=100.0, step=0.4, spread=0.5)
    plateau = d1[-16][4]
    for i in range(-15, -1):
        d1[i][1:5] = [plateau, plateau + 0.5, plateau - 0.5, plateau]
    d1[-1][1:5] = [plateau, plateau + 4.0, plateau - 0.2, plateau + 3.5]
    # Keep 4H already bullish so M3 does NOT flip on the same bar, letting M5 trigger cleanly
    h4 = _bars("4h", 160, end, start_price=120.0, step=0.3, spread=0.3)

    setup = macro_a1_setup({"4h": h4, "1d": d1}, MACRO_A1_STEADY)
    assert setup["strategy"] == MACRO_A1_STEADY
    assert setup["engine"] == "m5_1d_donchian"
    assert setup["side"] == "long"
    assert setup["timeframe"] == "1d"
    assert setup["hold_minutes"] == 720 * 60
