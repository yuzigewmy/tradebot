import asyncio
from decimal import Decimal
import gzip
import json

from spot_agent.contracts import TIMEFRAMES
from spot_agent.perpetual_book_history import sample_books
from spot_agent.perpetual_market import InstrumentRules, closed_windows
from spot_agent.perpetual_paper import PaperAccount, process_second
from spot_agent.perpetual_scan import aggregate_minutes, candidate_jobs


def test_bar_windows_match_live_at_hour_boundary(monkeypatch):
    minute, hour = TIMEFRAMES["1m"], TIMEFRAMES["1h"]
    end = 500 * hour
    bars = [[ts, 100, 102, 99, 101, 0.5] for ts in range(end - 162 * hour, end + hour, minute)]
    history = {"1m": bars, **{tf: list(aggregate_minutes(bars, TIMEFRAMES[tf])) for tf in ("15m", "1h")}}
    cuts = iter((end - minute, end, end + minute))

    def evaluate(windows):
        cut = next(cuts)
        assert windows == closed_windows(history, cut)
        assert windows["1h"][-1][0] + hour == cut // hour * hour
        return {"context": {}, "setup": {"strategy": "pullback"}}

    monkeypatch.setattr("spot_agent.perpetual_scan.detect_setup", evaluate)
    assert len(list(candidate_jobs("BTC-USDT-SWAP", bars, end - minute, end + 2 * minute))) == 3


def test_book_samples_only_use_updates_already_seen():
    packets = [
        {"action": "snapshot", "ts": "1", "bids": [["99", "3", "1"]], "asks": [["101", "1", "1"], ["102", "2", "1"]]},
        {"action": "update", "ts": "1200", "bids": [["100", "4", "1"]], "asks": [["101", "0", "0"]]},
        {"action": "update", "ts": "2200", "bids": [], "asks": [["101", "10", "1"]]},
    ]
    packed = gzip.compress(bytes(512) + b"".join(json.dumps(p, separators=(",", ":")).encode() + b"\n" for p in packets))
    result = sample_books([packed[:32], packed[32:]], [1000, 2000])
    assert result == {1000: [99, 101, 3, 1, 1], 2000: [100, 102, 4, 2, 1200]}


def paper():
    spec = InstrumentRules(*map(Decimal, ("0.01", "0.1", "0.01", "0.01", "1000", "0.004", "100")))
    return PaperAccount({"SWAP": spec})


def entry():
    return {"instrument": "SWAP", "candidate": {"side": "long", "strategy": "pullback", "risk_per_unit": 1,
            "closed_at": 0, "stop_loss": 99, "take_profit": 103},
            "verified_quote": {"ask": 100, "ask_size": 15, "mark": 100}}


def test_shared_account_opens_after_past_trades_then_uses_first_ordered_tick():
    async def scenario():
        account = paper()

        async def ticks(inst, ts):
            assert (inst, ts) == ("SWAP", 2000)
            return [[2100, 1, 103.25], [2400, 2, 98.8]]

        assert await process_second(account, {"SWAP": [1000, 100, 104, 98, 100]}, {}, [entry()], 2000, ticks) == ["entered"]
        assert account.positions["SWAP"].contracts == 10
        assert account.cash == Decimal("95.95")
        account.fund("SWAP", 0.001, 101, 2000)
        await process_second(account, {"SWAP": [2000, 100, 103.25, 98.8, 100, 1]}, {}, [], 3000, ticks)
        trade, = account.trades
        assert (trade["exit_reason"], trade["trigger_at"], trade["exit_evidence"]) == ("take_profit", 2100, "ordered_trades")
        assert account.equity() == Decimal("103.017390")

    asyncio.run(scenario())


def test_isolated_liquidation_has_priority_when_second_cannot_be_ordered():
    async def scenario():
        account = paper()
        assert account.enter("SWAP", entry()["candidate"], entry()["verified_quote"], 1000) == "entered"

        async def not_used(*_):
            raise AssertionError("liquidation cannot be resolved from the trade feed")

        await process_second(account, {"SWAP": [1000, 100, 100, 98, 98, 1]}, {"SWAP": [1000, 100, 100, 96, 96]}, [], 2000, not_used)
        trade, = account.trades
        assert trade["exit_reason"] == "liquidation" and trade["ambiguous_second"]
        assert account.equity() == Decimal("95.95")

    asyncio.run(scenario())


def test_time_exit_uses_first_trade_at_deadline_before_a_later_target():
    async def scenario():
        account = paper()
        signal = entry()
        signal["candidate"]["exit_at"] = 2000
        assert account.enter("SWAP", signal["candidate"], signal["verified_quote"], 1000) == "entered"

        async def ticks(inst, ts):
            assert (inst, ts) == ("SWAP", 3000)
            return [[3100, 1, 101], [3400, 2, 103.1]]

        await process_second(account, {"SWAP": [2000, 100.5, 100.5, 100.5, 100.5, 0]}, {}, [], 3000, ticks)
        assert "SWAP" in account.positions
        await process_second(account, {"SWAP": [3000, 101, 103.1, 101, 103.1, 1]}, {}, [], 4000, ticks)
        closed, = account.trades
        assert (closed["exit_reason"], closed["trigger_at"], closed["exit_reference"]) == ("time_limit", 3100, 101)

    asyncio.run(scenario())
