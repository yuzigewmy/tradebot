"""Load the historical inputs required by the perpetual strategy and executor."""

import asyncio
from bisect import bisect_right
from pathlib import Path

from .perpetual_history import DAY, MARK, MINUTE, SECOND, TRADE, dump, load
from .perpetual_market import settled_quote
from .perpetual_scan import candidate_jobs
from .perpetual_strategy import PROFILES

WARMUP = 161 * 60 * MINUTE
INITIAL_DELAY = 2 * SECOND
REFRESH_DELAY = SECOND


def jobs_path(root, instrument, profile):
    suffix = "" if profile == "v1" else f"-{PROFILES[profile][1]}"
    return root / f"{instrument}-jobs{suffix}.json"


async def prepare_market(history, instruments, start, end, root, profile="v1"):
    warm = start // (60 * MINUTE) * (60 * MINUTE) - WARMUP
    root.mkdir(parents=True, exist_ok=True)

    async def instrument_data(instrument):
        paths = {key: root / f"{instrument}-{key}.json" for key in ("trades", "marks", "funding")}
        candidates = jobs_path(root, instrument, profile)
        if not paths["trades"].exists():
            archives = await history.archives(2, warm, end, "monthly")
            dump(paths["trades"], await history.trade_minutes(instrument, warm, end, archives))
        if not paths["marks"].exists():
            dump(paths["marks"], await history.candles(MARK, instrument, MINUTE, start - MINUTE, end))
        if not paths["funding"].exists():
            dump(paths["funding"], await history.settled_funding(instrument, start, end))
        if not candidates.exists():
            dump(candidates, await asyncio.to_thread(lambda: list(candidate_jobs(instrument, load(paths["trades"]), start, end, profile))))
        return load(candidates)

    return [job for items in await asyncio.gather(*(instrument_data(inst) for inst in instruments)) for job in items]


def second_paths(root, job):
    t, inst = job["closed_at"], job["instrument"]
    return (root / f"{inst}-{t}-seconds.json", t)


async def prepare_seconds(history, jobs, root, end):
    root.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(18)

    async def fetch(job):
        path, t = second_paths(root, job)
        if path.exists():
            return
        async with sem:
            left, right = t - 15 * SECOND, min(t + MINUTE, end)
            instrument = job["instrument"]
            trades, marks = await asyncio.gather(
                history.candles(TRADE, instrument, SECOND, left, right, sparse=True),
                history.candles(MARK, instrument, SECOND, left, right, sparse=True),
            )
            dump(path, {"trade": trades, "mark": marks})

    await asyncio.gather(*(fetch(job) for job in sorted(jobs, key=lambda item: item["closed_at"])))


class EntryEvidence:
    def __init__(self, job, seconds, book, funding):
        self.start = job["closed_at"]
        self.trade = seconds["trade"]
        self.mark = seconds["mark"]
        self.trade_times = [row[0] + SECOND for row in self.trade]
        self.mark_times = [row[0] + SECOND for row in self.mark]
        self.book = book
        self.funding = funding

    def quote(self, now):
        top = self.book.get(str(now))
        trade_idx, mark_idx = bisect_right(self.trade_times, now) - 1, bisect_right(self.mark_times, now) - 1
        if top is None or trade_idx < 0 or mark_idx < 0:
            return None
        bid, ask, bid_size, ask_size, book_at = top
        trade, mark = self.trade[trade_idx], self.mark[mark_idx]
        return {
            "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
            "last": trade[4], "ticker_at": book_at, "last_at": self.trade_times[trade_idx],
            "mark": mark[4], "mark_at": self.mark_times[mark_idx],
            **settled_quote(now, self.funding),
        }

    def forming(self, now):
        bars = [row for row in self.trade if self.start <= row[0] < now]
        if bars:
            return [self.start, bars[0][1], max(r[2] for r in bars), min(r[3] for r in bars), bars[-1][4]]


class DayBooks:
    def __init__(self, root):
        self.root = Path(root)
        self.days = {}

    def get(self, job):
        instrument, ts = job["instrument"], job["closed_at"]
        key = instrument, ts // DAY * DAY
        if key not in self.days:
            path = self.root / f"{key[0]}-{key[1]}.json"
            if not path.exists():
                return None
            self.days[key] = load(path)
        return self.days[key]
