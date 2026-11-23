import asyncio
from decimal import Decimal

import pytest

from spot_agent.contracts import TIMEFRAMES
from spot_agent.perpetual_btc import BTC_INSTRUMENT, BTC_STRATEGY, live_btc_setup
from spot_agent.perpetual_market import InstrumentRules
from spot_agent.perpetual_monitor import PerpetualMonitor, SignalJournal
from spot_agent.wechat_research import signal_message

CLOSE = 1_795_392_000_000
MINUTE = TIMEFRAMES["1m"]


def candles(timeframe, sign):
    step = TIMEFRAMES[timeframe]
    rows = []
    for i in range(160):
        price = 100 + sign * (i - 159) * 0.1
        rows.append([CLOSE - (160 - i) * step, price, price + 0.3, price - 0.3, price, 10])
    return rows


@pytest.mark.parametrize(("side", "sign", "timeframe", "previous", "trigger", "expected_entry"), [
    ("long", 1, "1h", [99.9, 100.1, 97.5, 97.9, 10], [98, 101.6, 97.7, 101.2, 22], 100.3),
    ("short", -1, "30m", [100.1, 102.5, 99.9, 102.1, 10], [102, 102.3, 98.4, 98.8, 22], 99.9),
])
def test_btc_uses_closed_trend_and_entry_side_and_binds_child(tmp_path, monkeypatch, side, sign, timeframe, previous, trigger, expected_entry):
    windows = {tf: candles(tf, sign) for tf in ("1m", "15m", "30m", "1h", "4h")}
    windows[timeframe][-2][1:] = previous
    windows[timeframe][-1][1:] = trigger
    assert live_btc_setup(windows | {"4h": candles("4h", -sign)})["strategy"] is None
    clock = CLOSE + 200
    published = []

    async def sleep(delay):
        nonlocal clock
        clock += round(delay * 1000)

    async def quote(*_):
        assert clock == CLOSE + MINUTE
        return {"bid": 100, "ask": 100.2, "last": 100, "mark": 100, "bid_size": 10_000, "ask_size": 10_000,
                "funding_rate": 0.002, "funding_at": CLOSE + 4 * MINUTE, "ticker_at": clock, "mark_at": clock}

    monkeypatch.setattr("spot_agent.perpetual_monitor.now_ms", lambda: clock)
    monkeypatch.setattr("spot_agent.perpetual_monitor.asyncio.sleep", sleep)
    monkeypatch.setattr("spot_agent.perpetual_monitor.quote", quote)

    async def run():
        journal = SignalJournal(tmp_path / "signals.jsonl", published.append, follow_trades=True)
        monitor = PerpetualMonitor(BTC_INSTRUMENT, None, None, journal)
        monitor.mode, monitor.selected_strategy, monitor.status = "signals", BTC_STRATEGY, "online"
        monitor.bars, monitor.forming = windows, dict.fromkeys(windows)
        monitor.rules = InstrumentRules(*map(Decimal, (".1", ".01", ".01", ".01", "100000", ".004", "100")))
        monitor.last_evaluated = CLOSE - MINUTE
        monitor.ingest("4h", [CLOSE, 100, 200, 1, 1 if sign == 1 else 200, 999], False)
        monitor.evaluate()
        assert not published
        assert monitor.latest_job["trigger_candle"] == windows[timeframe][-1]
        await monitor.review(monitor.latest_job)

        signal, = published
        assert signal["side"] == side and signal["entry"] == expected_entry
        assert signal["entry_reference"] == ("best_ask" if sign == 1 else "best_bid")
        assert signal["exit_at"] == CLOSE + 2 * MINUTE
        assert monitor.view()["active_signal_id"] == signal["id"]
        assert "BTC 多周期均线确认" in signal_message(signal) and "250 USDT" in signal_message(signal)
        assert signal["trade_agent_id"].startswith("trade-")

    asyncio.run(run())
