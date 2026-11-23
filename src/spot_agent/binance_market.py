"""Public Binance USDⓈ-M market data for the perpetual monitor."""

import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from math import isfinite

from .perpetual_market import HISTORY_LIMIT, MarketError, now_ms

SYMBOLS = {"BTC-USDT-SWAP": "BTCUSDT", "ETH-USDT-SWAP": "ETHUSDT"}
DEMO_URL = "https://demo-fapi.binance.com"
LIVE_URL = "https://fapi.binance.com"


@dataclass(frozen=True)
class BinanceRules:
    tick: Decimal
    step: Decimal
    minimum: Decimal
    maximum: Decimal
    min_notional: Decimal

    def quantity(self, limit_price, notional, *, sizing_price):
        steps = (notional / sizing_price / self.step).to_integral_value(rounding=ROUND_FLOOR)
        quantity = steps * self.step
        if self.minimum <= quantity <= self.maximum and quantity * limit_price >= self.min_notional:
            return quantity


class BinanceMarket:
    def __init__(self, client, mode):
        self.client = client
        self.offset = 0
        self.base_url = DEMO_URL if mode == "demo" else LIVE_URL

    async def get(self, path, **params):
        async with self.client.get(self.base_url + path, params=params) as response:
            response.raise_for_status()
            return await response.json()

    async def rules(self):
        info = await self.get("/fapi/v1/exchangeInfo")
        available = {row["symbol"]: row for row in info["symbols"]}
        results = {}
        for instrument, symbol in SYMBOLS.items():
            row = available[symbol]
            if (row["status"], row["contractType"], row["marginAsset"]) != ("TRADING", "PERPETUAL", "USDT"):
                raise MarketError(f"币安 {symbol} 不是交易中的 USDT 永续")
            filters = {item["filterType"]: item for item in row["filters"]}
            lot = filters["LOT_SIZE"]
            results[instrument] = BinanceRules(*map(Decimal, (filters["PRICE_FILTER"]["tickSize"], lot["stepSize"],
                                                               lot["minQty"], lot["maxQty"], filters["MIN_NOTIONAL"]["notional"])))
        return results

    async def clock(self):
        return (await self.get("/fapi/v1/time"))["serverTime"]

    async def candles(self, instrument, timeframe, as_of, *, limit=HISTORY_LIMIT):
        items = await self.get("/fapi/v1/klines", symbol=SYMBOLS[instrument], interval=timeframe, limit=limit)
        closed, forming = [], None
        for start, opening, high, low, close, volume, end, *_ in items:
            row = [start, *map(float, (opening, high, low, close, volume))]
            _, o, h, l, c, v = row
            if not all(map(isfinite, row)) or not 0 < l <= min(o, c) <= max(o, c) <= h or v < 0:
                raise MarketError(f"币安 {instrument} {timeframe} K 线字段无效")
            if end < as_of:
                closed.append(row)
            else:
                forming = row
        return closed, forming

    async def quote(self, instrument):
        symbol = SYMBOLS[instrument]
        book, trade, premium = await asyncio.gather(
            self.get("/fapi/v1/ticker/bookTicker", symbol=symbol),
            self.get("/fapi/v2/ticker/price", symbol=symbol),
            self.get("/fapi/v1/premiumIndex", symbol=symbol),
        )
        values = {"bid": float(book["bidPrice"]), "ask": float(book["askPrice"]), "last": float(trade["price"]),
                  "bid_size": float(book["bidQty"]), "ask_size": float(book["askQty"]), "ticker_at": book["time"],
                  "mark": float(premium["markPrice"]), "mark_at": premium["time"],
                  "funding_rate": float(premium["lastFundingRate"]), "funding_at": premium["nextFundingTime"]}
        prices = [values[key] for key in ("bid", "ask", "last", "mark", "bid_size", "ask_size")]
        if not all(isfinite(price) and price > 0 for price in prices) or not isfinite(values["funding_rate"]):
            raise MarketError("币安盘口、标记价或资金费率无效")
        return values

    def now(self):
        return now_ms() + self.offset

    async def sync_clock(self, *, signing=False):
        before = now_ms()
        server = await self.clock()
        offset = server - (before + now_ms()) // 2
        if signing and not -500 <= offset <= 2500:
            raise MarketError("服务机时钟与币安差异超出签名窗口，请同步系统时钟")
        self.offset = offset
