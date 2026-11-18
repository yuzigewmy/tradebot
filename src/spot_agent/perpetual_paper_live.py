"""Run the same account rules on new OKX signals with public data only."""

import argparse
import asyncio
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
import httpx

from .agent import AgentError, model_configured
from .perpetual_api import INSTRUMENTS
from .perpetual_history import FUNDING_INTERVAL, MARK, SECOND, TRADE, OkxHistory
from .perpetual_market import instrument_rules, now_ms, quote
from .perpetual_monitor import PerpetualMonitor, SignalJournal
from .perpetual_paper import PaperAccount, process_second
from .perpetual_replay_data import INITIAL_DELAY, REFRESH_DELAY, settled_quote


class LivePaper:
    def __init__(self, history, rules, output):
        self.history, self.output = history, output
        self.account = PaperAccount(rules)
        self.pending = defaultdict(list)
        self.cursor = self.cutoff()

    @staticmethod
    def cutoff():
        return (now_ms() - 2 * SECOND) // SECOND * SECOND

    def write(self, name, rows):
        with (self.output / name).open("a", encoding="utf-8") as target:
            target.writelines(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)

    async def rates(self, inst):
        rows = await self.history.get("public/funding-rate-history", instId=inst, limit=3)
        return {int(row["fundingTime"]): float(row["realizedRate"]) for row in rows}

    async def market_quote(self, inst, closed_at=None):
        if closed_at is None:
            await asyncio.sleep(REFRESH_DELAY / SECOND)
        else:
            await asyncio.sleep(max(0, closed_at + INITIAL_DELAY - now_ms()) / SECOND)
        market, rates = await asyncio.gather(quote(self.history.client, inst), self.rates(inst))
        return market | settled_quote(now_ms(), rates)

    def enqueue(self, signal):
        at = (signal["published_at"] + SECOND - 1) // SECOND * SECOND
        if at >= signal["expires_at"]:
            self.write("entries.jsonl", [{"signal_at": signal["closed_at"], "instrument": signal["instrument"], "state": "expired"}])
        else:
            self.pending[at].append({"instrument": signal["instrument"], "candidate": signal, "verified_quote": signal["verified_quote"]})

    async def _bar(self, endpoint, inst, at):
        rows = await self.history.candles(endpoint, inst, SECOND, at - SECOND, at, sparse=endpoint == TRADE)
        return rows[0] if rows else None

    async def settle_funding(self, at):
        for inst in sorted(self.account.positions):
            async with asyncio.timeout(120):
                while at not in (rates := await self.rates(inst)):
                    await asyncio.sleep(3)
            self.account.fund(inst, rates[at], self.account.marks[inst], at)

    async def run(self):
        while True:
            deadline = self.cutoff()
            if not self.account.positions and not self.pending:
                self.cursor = deadline
            while self.cursor < deadline:
                at = self.cursor + SECOND
                active = sorted(self.account.positions)
                quotes = await asyncio.gather(*(self._bar(endpoint, inst, at) for endpoint in (TRADE, MARK) for inst in active))
                trades, marks = dict(zip(active, quotes[:len(active)], strict=True)), dict(zip(active, quotes[len(active):], strict=True))
                ready = sorted(self.pending.pop(at, []), key=lambda item: item["instrument"])
                before = len(self.account.events)
                states = await process_second(self.account, trades, marks, ready, at, self.history.trade_ticks)
                if at % FUNDING_INTERVAL == 0:
                    await self.settle_funding(at)
                if ready:
                    self.write("entries.jsonl", [{"at": at, "instrument": row["instrument"],
                                                "signal_at": row["candidate"]["closed_at"], "state": state}
                                               for row, state in zip(ready, states, strict=True)])
                if len(self.account.events) != before:
                    self.write("events.jsonl", self.account.events[before:])
                self.cursor = at
            await asyncio.sleep(.35)


async def run(output, duration):
    output.mkdir(parents=True)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=True) as market, httpx.AsyncClient() as model:
        specs = await asyncio.gather(*(instrument_rules(market, inst) for inst in INSTRUMENTS))
        paper = LivePaper(OkxHistory(market, output / "market"), dict(zip(INSTRUMENTS, specs, strict=True)), output)
        journal = SignalJournal(output / "signals.jsonl")
        monitors = [PerpetualMonitor(inst, market, model, journal, paper=paper) for inst in INSTRUMENTS]
        for monitor in monitors:
            monitor.start("changes")
        print(f"模拟账户已启动，账本：{output.resolve()}", flush=True)
        try:
            async with asyncio.timeout(duration):
                await paper.run()
        except TimeoutError:
            if duration is None:
                raise
        finally:
            await asyncio.gather(*(monitor.stop() for monitor in monitors))
            paper.write("session-end.jsonl", [{"at": paper.cursor, "equity": paper.account.equity(), "cash": paper.account.cash,
                                               "positions": [asdict(p) for p in paper.account.positions.values()]}])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/paper-live") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--duration", type=int, help="Seconds; by default the simulation runs until stopped")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    if not model_configured():
        raise AgentError("未配置模型，模拟账户未启动")
    asyncio.run(run(args.out, args.duration))


if __name__ == "__main__":
    main()
