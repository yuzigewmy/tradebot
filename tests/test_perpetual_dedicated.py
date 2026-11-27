from decimal import Decimal

from spot_agent.binance_market import BinanceRules
from spot_agent.binance_monitor import ALLOWED_STRATEGIES, STRATEGIES
from spot_agent.contracts import TIMEFRAMES
from spot_agent.perpetual_dedicated import (
    BTC_DAILY_DON20, BTC_SQUEEZE_DON14, DEDICATED_PROFILES, ETH_WIDE_ST_DON14, SOL_DAILY_ST30,
    dedicated_setup, dedicated_signal_candidate,
)


def _bars(timeframe, count, end, *, start_price=100.0, step=0.4, spread=0.6):
    dur = TIMEFRAMES[timeframe]
    rows = []
    for i in range(count):
        ts = end - (count - i) * dur
        p = start_price + i * step
        rows.append([ts, p - 0.1, p + spread, p - spread, p, 100.0])
    return rows


def test_default_live_strategies_match_d3_and_d6_is_selectable_for_btc():
    assert STRATEGIES == {
        "BTC-USDT-SWAP": BTC_SQUEEZE_DON14,
        "ETH-USDT-SWAP": ETH_WIDE_ST_DON14,
        "SOL-USDT-SWAP": SOL_DAILY_ST30,
    }
    assert BTC_SQUEEZE_DON14 in ALLOWED_STRATEGIES["BTC-USDT-SWAP"]
    assert BTC_DAILY_DON20 in ALLOWED_STRATEGIES["BTC-USDT-SWAP"]
    assert ETH_WIDE_ST_DON14 in ALLOWED_STRATEGIES["ETH-USDT-SWAP"]
    assert SOL_DAILY_ST30 in ALLOWED_STRATEGIES["SOL-USDT-SWAP"]


def test_btc_d3_squeeze_release_and_btc_d6_donchian20():
    end = 1795500000000 // TIMEFRAMES["1d"] * TIMEFRAMES["1d"]
    # Build tight compression on 4H (BB inside KC) then sharp upside breakout release
    d1 = _bars("1d", 160, end, start_price=100.0, step=0.4, spread=0.5)
    h4 = _bars("4h", 160, end, start_price=100.0, step=0.2, spread=2.0)
    flat = h4[-22][4]
    for i in range(-21, -1):
        h4[i][1:5] = [flat, flat + 0.15, flat - 0.15, flat]
    h4[-1][1:5] = [flat, flat + 6.0, flat - 0.1, flat + 5.5]

    setup_d3 = dedicated_setup({"4h": h4, "1d": d1}, BTC_SQUEEZE_DON14)
    assert setup_d3["strategy"] == BTC_SQUEEZE_DON14
    assert setup_d3["engine"] == "m7_4h_squeeze"
    assert setup_d3["side"] == "long"

    rules = BinanceRules(Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("10"), Decimal("50"))
    now = end + TIMEFRAMES["1m"] + 1000
    px = setup_d3["close"]
    quote = {"bid": px - 0.1, "ask": px + 0.1, "last": px, "mark": px,
             "bid_size": 10.0, "ask_size": 10.0, "ticker_at": now, "funding_at": now + 60_000}
    cand3, issue3 = dedicated_signal_candidate("BTC-USDT-SWAP", BTC_SQUEEZE_DON14, setup_d3, quote, rules, now, binance=True)
    assert issue3 is None
    assert cand3["leverage"] == 15 and cand3["risk_percent"] == 9.0
    assert abs(cand3["entry"] - cand3["stop_loss"]) / cand3["entry"] <= 0.0505

    # Build 20-day Donchian breakout for D6 BTC
    d1_d6 = _bars("1d", 160, end, start_price=100.0, step=0.4, spread=0.5)
    plateau = d1_d6[-22][4]
    for i in range(-21, -1):
        d1_d6[i][1:5] = [plateau, plateau + 0.5, plateau - 0.5, plateau]
    d1_d6[-1][1:5] = [plateau, plateau + 4.5, plateau - 0.2, plateau + 4.0]
    setup_d6 = dedicated_setup({"4h": h4, "1d": d1_d6}, BTC_DAILY_DON20)
    assert setup_d6["strategy"] == BTC_DAILY_DON20
    assert setup_d6["engine"] == "m2_1d_donchian20"
    assert setup_d6["side"] == "long"
    cand6, issue6 = dedicated_signal_candidate(
        "BTC-USDT-SWAP", BTC_DAILY_DON20, setup_d6,
        quote | {"bid": setup_d6["close"] - 0.1, "ask": setup_d6["close"] + 0.1, "last": setup_d6["close"], "mark": setup_d6["close"]},
        rules, now, binance=True,
    )
    assert issue6 is None
    assert cand6["leverage"] == 12 and cand6["risk_percent"] == 9.0


def test_eth_wide_supertrend_and_sol_daily_supertrend():
    end = 1795500000000 // TIMEFRAMES["1d"] * TIMEFRAMES["1d"]
    # ETH: 1D bullish, 4H Supertrend(14,3.8) flips from bearish to bullish
    d1_eth = _bars("1d", 160, end, start_price=100.0, step=0.5, spread=0.5)
    h4_eth = _bars("4h", 160, end, start_price=180.0, step=-0.2, spread=0.4)
    base = h4_eth[-2][4]
    h4_eth[-1][1:5] = [base, base + 15.0, base - 0.2, base + 14.0]
    setup_eth = dedicated_setup({"4h": h4_eth, "1d": d1_eth}, ETH_WIDE_ST_DON14)
    assert setup_eth["strategy"] == ETH_WIDE_ST_DON14
    assert setup_eth["engine"] == "m7_4h_st14_38"
    assert setup_eth["side"] == "long"

    # SOL: 1D Supertrend(10,3.0) flips from bearish to bullish on daily close
    d1_sol = _bars("1d", 160, end, start_price=200.0, step=-0.4, spread=0.6)
    sbase = d1_sol[-2][4]
    d1_sol[-1][1:5] = [sbase, sbase + 18.0, sbase - 0.2, sbase + 16.0]
    h4_sol = _bars("4h", 160, end, start_price=120.0, step=0.1, spread=0.4)
    setup_sol = dedicated_setup({"4h": h4_sol, "1d": d1_sol}, SOL_DAILY_ST30)
    assert setup_sol["strategy"] == SOL_DAILY_ST30
    assert setup_sol["engine"] == "m4_1d_st30"
    assert setup_sol["side"] == "long"
    assert DEDICATED_PROFILES[SOL_DAILY_ST30].leverage == Decimal("10")
    assert DEDICATED_PROFILES[SOL_DAILY_ST30].max_stop_pct == 0.085
