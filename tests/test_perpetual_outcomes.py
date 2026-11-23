import asyncio

import aiohttp
import pytest

from spot_agent.perpetual_history import MINUTE, SECOND
from spot_agent.perpetual_monitor import PerpetualMonitor, SignalJournal
from spot_agent.perpetual_outcomes import SignalOutcomes


def signal(**changes):
    return {
        "id": "BTC-USDT-SWAP:0:test", "instrument": "BTC-USDT-SWAP", "side": "short",
        "closed_at": 0, "published_at": 10_120, "expires_at": MINUTE, "exit_at": MINUTE,
        "stop_loss": 105, "take_profit": 90,
    } | changes


class Tape:
    def __init__(self, trades):
        self.trades = trades

    async def candles(self, endpoint, instrument, step, start, end):
        rows = []
        for at in range(start, end, step):
            prices = [price for ts, _, price in self.trades if at <= ts < at + step]
            if prices:
                rows.append([at, prices[0], max(prices), min(prices), prices[-1], len(prices)])
            else:
                rows.append([at, 100, 100, 100, 100, 0])
        return rows

    async def trade_ticks(self, instrument, at):
        return [trade for trade in self.trades if at <= trade[0] < at + SECOND]


@pytest.mark.parametrize("trades,expected", [
    ([(10_999, 1, 89), (11_200, 2, 106), (11_200, 3, 89)], ("stop_loss", 11_200, 106)),
    ([(11_200, 1, 89), (11_200, 2, 106)], ("take_profit", 11_200, 89)),
    ([(11_450, 1, 101), (61_200, 2, 101), (62_200, 3, 89)], ("time_limit", 61_200, 101)),
])
def test_signal_replays_first_eligible_exit_and_survives_restart(tmp_path, trades, expected):
    journal = SignalJournal(tmp_path / "signals.jsonl")
    record = signal()
    journal.append(record)
    tracker = SignalOutcomes(Tape(trades), journal)
    asyncio.run(tracker.update(record, 2 * MINUTE))

    restored = SignalJournal(journal.path)
    outcome = restored.outcomes[record["id"]]
    assert (outcome["status"], outcome["at"], outcome["price"]) == expected
    monitor = PerpetualMonitor(record["instrument"], None, None, restored)
    assert monitor.view()["signals"] == [record | {"outcome": outcome}]
    assert restored.for_instrument(record["instrument"]) == [record]


def test_signal_outcome_waits_on_missing_ticks_and_can_backfill(tmp_path):
    journal = SignalJournal(tmp_path / "signals.jsonl")
    record = signal()
    journal.append(record)
    tape = Tape([(15_000, 1, 89)])
    tracker = SignalOutcomes(tape, journal)

    class MissingTicks(Tape):
        async def trade_ticks(self, instrument, at):
            raise aiohttp.ClientConnectionError()

    tracker.history = MissingTicks(tape.trades)
    asyncio.run(tracker.update(record, MINUTE))
    assert journal.outcomes[record["id"]] == {"status": "unverified"}

    tracker.history = tape
    asyncio.run(tracker.update(record, MINUTE))
    assert journal.outcomes[record["id"]]["status"] == "take_profit"


def test_signal_cannot_enter_on_rounded_expiry_boundary(tmp_path):
    journal = SignalJournal(tmp_path / "signals.jsonl")
    record = signal(published_at=MINUTE - 1)
    journal.append(record)
    tracker = SignalOutcomes(Tape([(MINUTE, 1, 89)]), journal)
    asyncio.run(tracker.update(record, 2 * MINUTE))
    assert journal.outcomes[record["id"]] == {"status": "entry_expired"}
