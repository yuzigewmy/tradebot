from decimal import Decimal
import numpy as np

from spot_agent.binance_market import BinanceRules
from spot_agent.contracts import TIMEFRAMES
from spot_agent.perpetual_dual import (
    DUAL_COOLDOWN_MINUTES, DUAL_LEVERAGE, DUAL_RISK_PERCENT, DUAL_STRATEGY,
    dual_engine_setup, dual_signal_candidate,
)


def _trend_bars(timeframe, count, end, *, start_price=100.0, step=0.4, vol=10.0):
    dur = TIMEFRAMES[timeframe]
    rows = []
    for i in range(count):
        ts = end - (count - i) * dur
        p = start_price + i * step
        rows.append([ts, p - 0.1, p + 0.6, p - 0.6, p, vol])
    return rows


def test_dual_engine_triggers_s4_pullback_and_prices_48h_candidate():
    end = 1795500000000 // TIMEFRAMES["4h"] * TIMEFRAMES["4h"]
    h4 = _trend_bars("4h", 160, end, start_price=100.0, step=0.5)
    h1 = _trend_bars("1h", 160, end, start_price=140.0, step=0.25)
    # Create a sharp 3-bar pullback to drive RSI < 42, then a bullish reclaim above EMA20 and prior high
    peak = h1[-5][4]
    h1[-4][1:5] = [peak, peak + 0.2, peak - 6.0, peak - 5.5]
    h1[-3][1:5] = [peak - 5.5, peak - 5.0, peak - 11.0, peak - 10.5]
    h1[-2][1:5] = [peak - 10.5, peak - 9.0, peak - 14.0, peak - 13.0]
    h1[-1][1:6] = [peak - 13.0, peak + 5.0, peak - 13.2, peak + 4.0, 12.0]

    setup = dual_engine_setup({"4h": h4, "1h": h1})
    assert setup["strategy"] == DUAL_STRATEGY
    assert setup["engine"] == "s4_pullback"
    assert setup["side"] == "long"
    assert setup["breakeven_r"] == 1.2
    assert setup["trailing_atr"] == 0.0

    rules = BinanceRules(Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("10"), Decimal("20"))
    now = end + TIMEFRAMES["1m"] + 1000
    price = setup["close"]
    quote = {"bid": price - 0.05, "ask": price + 0.05, "last": price, "mark": price,
             "bid_size": 10.0, "ask_size": 10.0, "ticker_at": now, "funding_at": now + 60_000}
    cand, issue = dual_signal_candidate("ETH-USDT-SWAP", setup, quote, rules, now, binance=True)
    assert issue is None
    assert cand["strategy"] == DUAL_STRATEGY and cand["engine"] == "s4_pullback"
    assert cand["leverage"] == int(DUAL_LEVERAGE)
    assert cand["risk_percent"] == float(DUAL_RISK_PERCENT)
    assert cand["cooldown_minutes"] == DUAL_COOLDOWN_MINUTES
    # Must NOT cut off 2 minutes before funding settlement (48h full hold)
    assert cand["exit_at"] == end + TIMEFRAMES["1m"] + 48 * TIMEFRAMES["1h"]
    assert cand["stop_loss"] < cand["entry"] < cand["take_profit"]
    # 30x isolated liquidation safety: stop distance must remain strictly capped <= 2.4%
    assert abs(cand["entry"] - cand["stop_loss"]) / cand["entry"] <= 0.0245


def test_dual_engine_triggers_s3_keltner_short_breakout():
    end = 1795500000000 // TIMEFRAMES["4h"] * TIMEFRAMES["4h"]
    h4 = _trend_bars("4h", 160, end, start_price=220.0, step=-0.5)
    h1 = _trend_bars("1h", 160, end, start_price=180.0, step=-0.2)
    # Keep previous close near EMA, then break below EMA20 - 2*ATR on high volume
    base = h1[-2][4]
    h1[-2][1:6] = [base, base + 0.3, base - 0.3, base, 10.0]
    h1[-1][1:6] = [base, base + 0.2, base - 6.0, base - 5.5, 18.0]

    setup = dual_engine_setup({"4h": h4, "1h": h1})
    assert setup["strategy"] == DUAL_STRATEGY
    assert setup["engine"] == "s3_keltner"
    assert setup["side"] == "short"
    assert setup["breakeven_r"] == 1.5
    assert setup["trailing_atr"] == 2.2


def test_binance_trade_uses_risk_sizing_and_trails_stop_safely(tmp_path, monkeypatch):
    import asyncio
    import sqlite3
    from spot_agent.binance_execution import BinanceTrade, TradeLedger

    now = 1795500000000
    signal = {
        "id": "ETH:dual-1", "instrument": "ETH-USDT-SWAP", "trade_agent_id": "trade-dual",
        "strategy": DUAL_STRATEGY, "engine": "s3_keltner", "side": "long",
        "entry": 2000.0, "stop_loss": 1960.0, "take_profit": 2180.0,
        "atr": 20.0, "breakeven_r": 1.5, "trailing_atr": 2.2,
        "risk_percent": 3.0, "leverage": 10, "max_notional_multiple": 5.0,
        "expires_at": now + 10_000, "exit_at": now + 48 * TIMEFRAMES["1h"],
        "verified_quote": {"funding_at": now + 8 * TIMEFRAMES["1h"]},
    }
    calls, notices = [], []
    algos = {}
    position = {"positionAmt": "0.037", "entryPrice": "2000", "markPrice": "2000", "liquidationPrice": "1820"}

    class Exchange:
        async def call(self, action, *args):
            calls.append((action, args))
            if action == "prepare_risk":
                assert args[0] == "ETHUSDT" and args[1] == 10 and args[3] == Decimal("3")
                return Decimal("7.4"), Decimal("74")
            if action == "enter":
                assert args[3] == Decimal("0.037")
                return {"orderId": 99, "status": "FILLED", "executedQty": "0.037", "updateTime": now}
            if action == "position":
                return position
            if action == "protect":
                algos[args[4]] = {"algoStatus": "NEW", "trigger": args[2]}
                return algos[args[4]]
            if action == "algo":
                return algos[args[0]]
            if action == "cancel":
                algos[args[0]]["algoStatus"] = "CANCELED"
                return
            raise AssertionError(f"unexpected {action}")

    class Market:
        def now(self):
            return now

        async def quote(self, inst):
            return {"bid": 1999.9, "ask": 2000.0, "last": 2000.0, "mark": 2000.0,
                    "ask_size": 5.0, "bid_size": 5.0, "ticker_at": now}

    monkeypatch.setattr("spot_agent.binance_execution.now_ms", lambda: now)
    rules = BinanceRules(Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("10"), Decimal("20"))

    async def scenario():
        with sqlite3.connect(tmp_path / "dual.sqlite") as conn:
            ledger = TradeLedger(conn, "acct")
            ledger.add(signal)
            worker = BinanceTrade(signal, {}, ledger, Exchange(), Market(), rules, notices.append, "demo")
            await worker.step()
            assert worker.state["protected"] is True
            # Move mark price up +70 USDT (> 1.5R = 60) -> should trigger breakeven/trail stop move!
            position["markPrice"] = "2070"
            await worker.step()
            assert worker.state["stop_rev"] == 1
            assert worker.state["active_stop"] > 2000.0
            assert algos[worker.protections["stop"]]["algoStatus"] == "CANCELED"
            assert algos[worker.protection_ids()["stop"]]["algoStatus"] == "NEW"
            assert any(n.kind == "trail_1" for n in notices)

    asyncio.run(scenario())
