import asyncio
from decimal import Decimal

import pytest

from spot_agent.contracts import TIMEFRAMES
from spot_agent.perpetual_compression import COMPRESSION_STRATEGY, compression_setup
from spot_agent.perpetual_market import InstrumentRules
from spot_agent.perpetual_monitor import PerpetualMonitor, SignalJournal
from spot_agent.wechat_research import signal_message

MINUTE = 60_000
CLOSE = 1_795_395_600_000 // (30 * MINUTE) * (30 * MINUTE)


def series(tf, end):
    step = TIMEFRAMES[tf]
    return [[end - (160 - i) * step, 100, 101, 99, 100, 10] for i in range(160)]


def compression_bars():
    candles = series("30m", CLOSE)
    for row in candles[:140]:
        row[2:4] = [102, 98]
    for row in candles[140:-1]:
        row[2:4] = [100.2, 99.8]
    candles[-1][1:] = [100, 100, 97.4, 97.5, 20]
    return candles


def test_compression_requires_both_prior_contraction_and_closed_volume_breakout():
    candles = compression_bars()
    assert compression_setup(candles)["strategy"] == COMPRESSION_STRATEGY
    candles[-1][-1] = 10
    setup = compression_setup(candles)
    assert setup["range_cross"] and setup["compressed"] and setup["strategy"] is None


@pytest.mark.parametrize("depth,publish", [(10_000, True), (1, False)])
def test_eth_compression_waits_one_minute_checks_depth_and_publishes_followable_signal(tmp_path, monkeypatch, depth, publish):
    now = CLOSE + 200
    prices = {"bid": 97.60, "ask": 97.61, "last": 97.60, "mark": 97.60, "bid_size": depth, "ask_size": depth,
              "funding_rate": .001, "funding_at": CLOSE + 4 * MINUTE}
    sent = []

    async def market_quote(*_):
        assert now == CLOSE + MINUTE
        return prices | {"ticker_at": now, "mark_at": now}

    async def sleep(delay):
        nonlocal now
        now += round(delay * 1000)

    monkeypatch.setattr("spot_agent.perpetual_monitor.now_ms", lambda: now)
    monkeypatch.setattr("spot_agent.perpetual_monitor.quote", market_quote)
    monkeypatch.setattr("spot_agent.perpetual_monitor.asyncio.sleep", sleep)

    async def run():
        journal = SignalJournal(tmp_path / "signals.jsonl", sent.append, follow_trades=True)
        monitor = PerpetualMonitor("ETH-USDT-SWAP", None, None, journal)
        monitor.mode, monitor.selected_strategy, monitor.status = "signals", COMPRESSION_STRATEGY, "online"
        monitor.rules = InstrumentRules(*map(Decimal, (".01", ".1", ".01", ".01", "5000", ".004", "100")))
        monitor.last_evaluated = CLOSE - 30 * MINUTE
        monitor.bars = {tf: series(tf, CLOSE) for tf in ("1m", "15m", "1h")}
        monitor.bars["30m"] = compression_bars()
        monitor.forming = dict.fromkeys(monitor.bars)
        monitor.evaluate()
        assert monitor.latest_job["closed_at"] == CLOSE
        assert not sent
        await monitor.review(monitor.latest_job)
        assert bool(sent) is publish
        if publish:
            signal, = sent
            assert signal["exit_at"] == CLOSE + 2 * MINUTE
            assert signal["entry"] == 97.58
            assert signal["take_profit"] < signal["entry"] < signal["stop_loss"]
            for field in ("take_profit", "stop_loss"):
                assert Decimal(str(signal[field])) % monitor.rules.tick == 0
            assert "250 USDT" in signal_message(signal)
            assert monitor.view()["active_signal_id"] == signal["id"]
            assert signal["trade_agent_id"].startswith("trade-")
        else:
            assert "盘口深度" in monitor.review_state["note"]

    asyncio.run(run())
