"""Public OKX history for closed-bar analysis and event replay."""

import asyncio
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import time
from zipfile import ZipFile

import aiohttp

from .perpetual_market import MarketError, OKX_HTTP, parse_candle

SECOND = 1000
MINUTE = 60 * SECOND
DAY = 1440 * MINUTE
FUNDING_INTERVAL = 8 * 60 * MINUTE
TRADE_ARCHIVE_TZ = timezone(timedelta(hours=8))
PAGE = 300
TRADE = "market/history-candles"
MARK = "market/history-mark-price-candles"
ARCHIVE = "public/market-data-history"


def utc(ts):
    return datetime.fromtimestamp(ts / SECOND, timezone.utc).isoformat(timespec="milliseconds")


def dump(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class OkxHistory:
    def __init__(self, client, root):
        self.client = client
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.next_request = {}

    async def get(self, endpoint, **params):
        for attempt in range(4):
            now = time.monotonic()
            due = max(now, self.next_request.get(endpoint, now))
            self.next_request[endpoint] = due + 0.112
            await asyncio.sleep(due - now)
            try:
                async with self.client.get(f"{OKX_HTTP}/api/v5/{endpoint}", params=params) as response:
                    if response.status not in (429, 500, 502, 503, 504):
                        response.raise_for_status()
                        body = await response.json()
                        if body["code"] == "0":
                            return body["data"]
                        if body["code"] != "50011":
                            raise MarketError(f"OKX {endpoint}: {body['code']} {body['msg']}")
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError):
                if attempt == 3:
                    raise
            await asyncio.sleep(2 ** attempt)
        raise MarketError(f"OKX {endpoint}: 请求次数已用尽")

    async def archives(self, module, start, end, aggregation):
        result = await self.get(ARCHIVE, module=module, instType="SWAP", instFamilyList="BTC-USDT,ETH-USDT",
                                dateAggrType=aggregation, begin=start, end=end - 1)
        return [file for item in result for group in item["details"] for file in group["groupDetails"]]

    async def zip_rows(self, file):
        path = self.root / file["filename"]
        if not path.exists():
            async with self.client.get(file["url"]) as response:
                response.raise_for_status()
                content = await response.read()
            temp = path.with_suffix(".part")
            temp.write_bytes(content)
            temp.replace(path)
        with ZipFile(path) as archive, archive.open(archive.namelist()[0]) as member:
            return list(csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig")))

    async def candles(self, endpoint, instrument, step, start, end, *, sparse=False):
        """Fetch half-open windows. OKX `after` is an exclusive upper cursor."""
        key = "mark" if endpoint == MARK else "trade"
        bar = {SECOND: "1s", MINUTE: "1m"}[step]
        cache = self.root / key / instrument / bar
        cache.mkdir(parents=True, exist_ok=True)

        async def page(left, right):
            path = cache / f"{left}-{right}.json"
            if path.exists():
                return load(path)
            raw = await self.get(endpoint, instId=instrument, bar=bar, after=right, limit=PAGE)
            candles = []
            for old in reversed(raw):
                ts = int(old[0])
                if not left <= ts < right:
                    continue
                row, closed = (parse_candle(old) if endpoint == TRADE else
                               ([ts, *map(float, old[1:5])], old[5] == "1"))
                if closed:
                    candles.append(row)
            validate_times(candles, step, left, right, sparse)
            dump(path, candles)
            return candles

        chunks = [(left, min(left + step * PAGE, end)) for left in range(start, end, step * PAGE)]
        return [row for part in await asyncio.gather(*(page(*interval) for interval in chunks)) for row in part]

    async def trade_minutes(self, instrument, start, end, archives):
        rows = {}
        for file in archives:
            if not file["filename"].startswith(instrument + "-"):
                continue
            for raw in await self.zip_rows(file):
                ts = int(raw["open_time"])
                if start <= ts < end:
                    row, closed = parse_candle([raw[key] for key in ("open_time", "open", "high", "low", "close", "vol", "vol_ccy", "vol_quote", "confirm")])
                    if closed:
                        rows[ts] = row
        if not rows:
            raise MarketError(f"{instrument} 未取得月度 K 线档案")
        # Overlap the seam to independently check the archive's bar definition.
        recent = await self.candles(TRADE, instrument, MINUTE, max(rows) - 2 * MINUTE, end)
        for row in recent:
            if row[0] in rows and rows[row[0]] != row:
                raise MarketError(f"{instrument} 月度 K 线与 REST 不一致: {utc(row[0])}")
            rows[row[0]] = row
        series = [rows[ts] for ts in sorted(rows)]
        validate_times(series, MINUTE, start, end, False)
        return series

    async def settled_funding(self, instrument, start, end):
        path = self.root / f"{instrument}-funding-{start}-{end}.json"
        if path.exists():
            return load(path)
        cursor, rates = end, []
        first = start // FUNDING_INTERVAL * FUNDING_INTERVAL - FUNDING_INTERVAL
        while cursor > first:
            data = await self.get("public/funding-rate-history", instId=instrument, after=cursor, limit=100)
            if not data or not int(data[-1]["fundingTime"]) < cursor:
                raise MarketError(f"{instrument} 资金费率档案提前结束")
            cursor = int(data[-1]["fundingTime"])
            rates.extend([int(row["fundingTime"]), float(row["realizedRate"])] for row in data if first <= int(row["fundingTime"]) < end)
        rates.reverse()
        validate_times(rates, FUNDING_INTERVAL, first, end, False)
        dump(path, rates)
        return rates

    async def trade_ticks(self, instrument, start):
        """Return the matching-engine order for the specified historical second."""
        path = self.root / "ticks" / instrument / f"{start}.json"
        if path.exists():
            return load(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        end = start + SECOND
        endpoint = "market/history-trades"
        raw = await self.get(endpoint, instId=instrument, type=2, after=end, limit=100)
        rows = []
        while raw:
            rows.extend([int(r["ts"]), int(r["tradeId"]), float(r["px"])] for r in raw if start <= int(r["ts"]) < end)
            if len(raw) < 100 or int(raw[-1]["ts"]) < start:
                break
            raw = await self.get(endpoint, instId=instrument, type=1, after=raw[-1]["tradeId"], limit=100)
        if not rows:
            date = datetime.fromtimestamp(start / SECOND, TRADE_ARCHIVE_TZ).date()
            filename = f"{instrument}-trades-{date}.zip"
            url = f"https://static.okx.com/cdn/okex/traderecords/trades/daily/{date:%Y%m%d}/{filename}?v=1001"
            rows = await self.archived_ticks(filename, url, start, end)
        rows.sort()
        dump(path, rows)
        return rows

    async def archived_ticks(self, filename, url, start, end):
        path = self.root / filename
        if not path.exists():
            async with self.client.get(url) as response:
                response.raise_for_status()
                body = await response.read()
            temp = path.with_suffix(".part")
            temp.write_bytes(body)
            temp.replace(path)

        def select():
            # The trade CSV is ordered by execution time and trade ID.
            with ZipFile(path) as archive, archive.open(archive.namelist()[0]) as member:
                rows = []
                for raw in csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig")):
                    ts = int(raw["created_time"])
                    if ts >= end:
                        break
                    if ts >= start:
                        rows.append([ts, int(raw["trade_id"]), float(raw["price"])])
            return rows

        return await asyncio.to_thread(select)


def validate_times(rows, step, start, end, sparse):
    expected = range(start, end, step)
    times = [row[0] for row in rows]
    if sparse:
        valid = times == sorted(set(times)) and all(ts in expected for ts in times)
    else:
        valid = times == list(expected)
    if not valid:
        missing = sorted(set(expected) - set(times))
        first = utc(missing[0]) if missing else "无"
        raise MarketError(f"OKX {utc(start)} 至 {utc(end)} 间隔 {step}ms 数据不连续：{len(missing)} 缺口，首个 {first}")
