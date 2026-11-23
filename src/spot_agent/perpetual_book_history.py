"""Extract as-of top-of-book quotes from OKX's official L2 archives."""

import asyncio
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import zlib

import httpx

from .perpetual_history import DAY, MINUTE, SECOND, dump, load

STAMP = re.compile(rb'"ts":"([0-9]+)"')
BOOK_SNAPSHOT_INTERVAL = 15 * MINUTE


def archive_url(instrument, day):
    date = datetime.fromtimestamp(day / SECOND, timezone.utc).date()
    return f"https://static.okx.com/cdn/okx/match/orderbook/pro/L2/400lv/daily/{date:%Y%m%d}/{instrument}-L2orderbook-400lv-{date}.tar.gz"


def sample_books(blocks, times, depth=1):
    """Each file begins with a TAR header and offers a fresh snapshot every 15 minutes."""
    if not times:
        return {}
    asks, bids, sampled = {}, {}, {}
    pos, header_left, book_time, active_window = 0, 512, None, None
    partial = b""
    decoder = zlib.decompressobj(31)
    for block in blocks:
        data = decoder.decompress(block)
        if header_left:
            drop = min(header_left, len(data))
            data = data[drop:]
            header_left -= drop
        lines = (partial + data).split(b"\n")
        partial = lines.pop()
        for line in lines:
            match = STAMP.search(line)
            if match is None:
                continue
            ts = int(match[1])
            while pos < len(times) and times[pos] < ts:
                at = times[pos]
                if asks and bids and book_time is not None and 0 <= at - book_time <= 15 * SECOND:
                    ask, bid = min(asks), max(bids)
                    snapshot = [bid, ask, bids[bid], asks[ask], book_time]
                    if depth > 1:
                        bid_levels = [(price, bids[price]) for price in sorted(bids, reverse=True)[:depth]]
                        ask_levels = [(price, asks[price]) for price in sorted(asks)[:depth]]
                        snapshot.extend((bid_levels, ask_levels))
                    sampled[at] = snapshot
                pos += 1
            if pos == len(times):
                return sampled
            if ts // BOOK_SNAPSHOT_INTERVAL != times[pos] // BOOK_SNAPSHOT_INTERVAL:
                continue
            is_snapshot = b'"action":"snapshot"' in line
            if is_snapshot:
                asks.clear()
                bids.clear()
                active_window = ts // BOOK_SNAPSHOT_INTERVAL
            if active_window != ts // BOOK_SNAPSHOT_INTERVAL:
                continue
            packet = json.loads(line)
            for book, key in ((asks, "asks"), (bids, "bids")):
                for price, size, _ in packet[key]:
                    price, size = float(price), float(size)
                    if size:
                        book[price] = size
                    else:
                        book.pop(price, None)
            book_time = ts
    return sampled


def acquire_day(instrument, day, times, path, depth=1):
    if path.exists():
        return
    url = archive_url(instrument, day)
    with httpx.stream("GET", url, timeout=40, follow_redirects=True) as response:
        if response.status_code == 404:
            result = {"url": url, "status": "unpublished", "quotes": {}}
        else:
            response.raise_for_status()
            result = {"url": url, "status": "ok", "quotes": sample_books(response.iter_raw(256 * 1024), times, depth)}
    dump(path, result)
    print(f"L2 {instrument} {url[-17:-7]} {len(result['quotes'])}/{len(times)} {result['status']}", flush=True)


async def acquire_books(jobs, root, workers=8):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    days = defaultdict(set)
    for job in jobs:
        ts, inst = job["closed_at"], job["instrument"]
        days[inst, ts // DAY * DAY].update(range(ts + SECOND, ts + MINUTE, SECOND))
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        ordered = sorted(days.items(), key=lambda item: (-item[0][1], item[0][0]))
        await asyncio.gather(*(loop.run_in_executor(pool, acquire_day, instrument, day, sorted(times), root / f"{instrument}-{day}.json")
                               for (instrument, day), times in ordered))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    options = parser.parse_args()
    asyncio.run(acquire_books([job for path in options.jobs for job in load(path)], options.out, options.workers))
