"""Chronological replay for the shared perpetual paper account."""

import asyncio
from collections import Counter, defaultdict

from .perpetual_history import MARK, MINUTE, SECOND, TRADE, load
from .perpetual_paper import PaperAccount, d, exit_trigger, process_second
from .perpetual_replay_data import second_paths
from .perpetual_strategy import signal_cooldown


def publish_replay_signals(reviews, latest=None, *, profile="v1"):
    if latest is None:
        latest = {}
    signals, signal_counts = [], Counter()
    for review in sorted(reviews, key=lambda row: (row["closed_at"], row["instrument"])):
        at, inst = review["closed_at"], review["instrument"]
        if signal_cooldown(latest.get(inst), at, profile=profile):
            signal_counts["cooldown"] += 1
        elif review["status"] == "passed":
            signals.append(review)
            latest[inst] = at
            signal_counts["published"] += 1
        else:
            signal_counts[review["status"]] += 1
    return signals, signal_counts


class Replay:
    def __init__(self, history, seconds, rules, trades, marks, funding, start, end):
        self.history, self.seconds = history, seconds
        self.account = PaperAccount(rules)
        self.trades = {inst: {row[0]: row for row in rows} for inst, rows in trades.items()}
        self.marks = {inst: {row[0]: row for row in rows} for inst, rows in marks.items()}
        self.funding, self.start, self.end = funding, start, end
        self.equity, self.entry_log = [], []
        self.minute_mismatches = []
        self.cursor = start
        self.sample(start)

    async def _seconds(self, inst, t):
        path, _ = second_paths(self.seconds, {"instrument": inst, "closed_at": t})
        if path.exists():
            data = load(path)
            trades, marks = data["trade"], data["mark"]
        else:
            trades, marks = await asyncio.gather(
                self.history.candles(TRADE, inst, SECOND, t, t + MINUTE, sparse=True),
                self.history.candles(MARK, inst, SECOND, t, t + MINUTE, sparse=True),
            )
        return ({r[0] + SECOND: r for r in trades if t <= r[0] < t + MINUTE},
                {r[0] + SECOND: r for r in marks if t <= r[0] < t + MINUTE})

    def sample(self, at):
        self.equity.append((at, self.account.equity(), self.account.cash, len(self.account.positions)))

    async def run(self, signals):
        return await self.advance(signals, self.end)

    async def advance(self, signals, through):
        by_minute = defaultdict(list)
        for signal in signals:
            by_minute[signal["closed_at"]].append(signal)
        for t in range(self.cursor, through, MINUTE):
            active = set(self.account.positions)
            for inst in sorted(active):
                if t in self.funding[inst]:
                    self.account.fund(inst, self.funding[inst][t], self.marks[inst][t - MINUTE][4], t)
            entries = by_minute[t]
            coarse_hit = set()
            for inst in active:
                position = self.account.positions[inst]
                if (position.exit_at is not None and position.exit_at < t + MINUTE
                        or exit_trigger(position, self.trades[inst][t], self.marks[inst][t])):
                    coarse_hit.add(inst)
            if entries or coarse_hit:
                symbols = sorted(active | {row["instrument"] for row in entries})
                series = dict(zip(symbols, await asyncio.gather(*(self._seconds(inst, t) for inst in symbols)), strict=True))
                ordered = defaultdict(list)
                for signal in entries:
                    ordered[signal["verified_at"]].append(signal)
                for at in range(t + SECOND, t + MINUTE + SECOND, SECOND):
                    next_entries = sorted(ordered[at], key=lambda row: row["instrument"])
                    trades = {inst: pair[0].get(at) for inst, pair in series.items()}
                    marks = {inst: pair[1].get(at) for inst, pair in series.items()}
                    before = len(self.account.events)
                    states = await process_second(self.account, trades, marks, next_entries, at, self.history.trade_ticks)
                    for row, state in zip(next_entries, states, strict=True):
                        self.entry_log.append({"instrument": row["instrument"], "signal_at": row["closed_at"],
                                               "entry_checked_at": at, "state": state})
                    if len(self.account.events) != before:
                        self.sample(at)
                for inst in sorted(coarse_hit & set(self.account.positions)):
                    if self.account.positions[inst].opened_at < t:
                        self.minute_mismatches.append({"instrument": inst, "minute": t})
            for inst in self.account.positions:
                self.account.marks[inst] = d(self.marks[inst][t][4])
            self.sample(t + MINUTE)
        self.cursor = through
        return self
