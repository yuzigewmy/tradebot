"""OKX trade candles with exchange confirmation, and public reference prices."""

from decimal import Decimal
from math import isfinite
import asyncio
import time

from .contracts import TIMEFRAMES

OKX_HTTP = "https://www.okx.com"
OKX_CANDLES_WS = "wss://ws.okx.com/ws/v5/business"
BARS = {"1m": "1m", "15m": "15m", "1h": "1H"}
CHANNELS = {f"candle{bar}": timeframe for timeframe, bar in BARS.items()}
WINDOW = 160
HISTORY_LIMIT = 240


class MarketError(RuntimeError):
    pass


def now_ms():
    return time.time_ns() // 1_000_000


def parse_candle(raw):
    ts, opening, high, low, close, _, base_volume, _, confirm = raw
    row = [int(ts), *map(float, (opening, high, low, close, base_volume))]
    _, o, h, l, c, v = row
    if confirm not in ("0", "1") or not all(map(isfinite, row)) or not 0 < l <= min(o, c) <= max(o, c) <= h or v < 0:
        raise MarketError("OKX K 线字段无效")
    return row, confirm == "1"


def closed_windows(bars, end):
    windows = {}
    for timeframe, history in bars.items():
        duration = TIMEFRAMES[timeframe]
        cutoff = end // duration * duration
        rows = [row for row in history if row[0] < cutoff][-WINDOW:]
        if len(rows) != WINDOW or any(row[0] != cutoff - (WINDOW - i) * duration for i, row in enumerate(rows)):
            return None
        windows[timeframe] = rows
    return windows


async def public_data(client, path, **params):
    async with client.get(f"{OKX_HTTP}/api/v5/{path}", params=params) as response:
        response.raise_for_status()
        body = await response.json()
    if body["code"] != "0" or not body["data"]:
        raise MarketError(f"OKX {path} 无数据（{body['code']}）")
    return body["data"]


async def instrument_tick(client, instrument):
    contract, = await public_data(client, "public/instruments", instType="SWAP", instId=instrument)
    if (contract["ctType"], contract["settleCcy"], contract["state"]) != ("linear", "USDT", "live"):
        raise MarketError("仅支持交易中的线性 USDT 永续")
    tick = Decimal(contract["tickSz"])
    if not tick.is_finite() or tick <= 0:
        raise MarketError("OKX 合约报价精度无效")
    return tick


async def history(client, instrument, timeframe):
    raw = await public_data(client, "market/candles", instId=instrument, bar=BARS[timeframe], limit=HISTORY_LIMIT)
    parsed = [parse_candle(candle) for candle in reversed(raw)]
    closed = [row for row, confirmed in parsed if confirmed]
    if len(closed) < WINDOW:
        raise MarketError(f"OKX {timeframe} 历史不足 {WINDOW} 根")
    return closed, next((row for row, confirmed in reversed(parsed) if not confirmed), None)


async def quote(client, instrument):
    (ticker,), (mark,), (funding,) = await asyncio.gather(
        public_data(client, "market/ticker", instId=instrument),
        public_data(client, "public/mark-price", instType="SWAP", instId=instrument),
        public_data(client, "public/funding-rate", instId=instrument),
    )
    values = {
        "bid": float(ticker["bidPx"]), "ask": float(ticker["askPx"]), "last": float(ticker["last"]), "ticker_at": int(ticker["ts"]),
        "mark": float(mark["markPx"]), "mark_at": int(mark["ts"]),
        "funding_rate": float(funding["fundingRate"]), "funding_at": int(funding["fundingTime"]),
    }
    prices = [values[key] for key in ("bid", "ask", "last", "mark")]
    if not all(price > 0 and isfinite(price) for price in prices) or not isfinite(values["funding_rate"]):
        raise MarketError("OKX 盘口、标记价或资金费率无效")
    return values
