"""Public spot OHLCV; all frames in a report share one UTC cutoff.

Source: https://github.com/ccxt/ccxt/wiki/Manual#ohlcv-candlestick-charts
"""

from dataclasses import dataclass
import math
import time

import ccxt
import numpy as np
import pandas as pd

from .contracts import TIMEFRAMES


@dataclass
class MarketFrame:
    exchange: str
    symbol: str
    timeframe: str
    source: str
    as_of: int
    fetched_at: int
    rows: list
    quality: dict


def build_frame(rows, exchange, symbol, timeframe, source, as_of):
    """Validate external OHLCV once, then keep only closed candles."""
    duration = TIMEFRAMES[timeframe]
    previous = -1
    for row in rows:
        if len(row) != 6 or not all(math.isfinite(v) for v in row):
            raise ValueError("OHLCV 必须包含6个有限数值")
        ts, opening, high, low, close, volume = row
        if ts <= previous or ts % duration != 0:
            raise ValueError("K线时间必须按UTC周期对齐且严格递增")
        if not (0 < low <= min(opening, close) <= max(opening, close) <= high) or volume < 0:
            raise ValueError("OHLCV 价格范围或成交量不合法")
        previous = ts
    closed = [r for r in rows if r[0] + duration <= as_of]
    if not closed:
        raise ValueError("截止时点没有已收盘K线")
    gap_indices = [i for i in range(1, len(closed)) if closed[i][0] - closed[i - 1][0] != duration]
    tail_start = gap_indices[-1] if gap_indices else 0
    quality = {
        "closed_count": len(closed),
        "excluded_open": len(rows) - len(closed),
        "gap_count": len(gap_indices),
        "contiguous_count": len(closed) - tail_start,
        "tail_start": tail_start,
        "last_closed_at": int(closed[-1][0] + duration),
        "stale": closed[-1][0] + duration < as_of // duration * duration,
    }
    return MarketFrame(exchange, symbol, timeframe, source, as_of, int(time.time() * 1000), closed, quality)


def fetch_market(exchange_id, symbol, count=600):
    exchange = getattr(ccxt, exchange_id)({
        "enableRateLimit": True,
        "timeout": 15000,
        "options": {
            "defaultType": "spot", "fetchMarkets": {"types": ["spot"]},
            "fetchOHLCV": {"timezone": "UTC"},
        },
    })
    exchange.load_markets()
    if not exchange.market(symbol)["spot"]:
        raise ValueError("只支持现货市场")
    as_of = exchange.fetch_time() - 2000
    frames = {}
    for timeframe, duration in TIMEFRAMES.items():
        since = (as_of // duration - count) * duration
        rows = []
        limit = 300 if exchange_id == "okx" else 1000
        for _ in range(4):
            page = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            if not page:
                break
            rows.extend(r for r in page if r[0] >= since)
            next_since = page[-1][0] + duration
            if next_since <= since:
                raise ValueError("交易所历史K线分页游标未前进")
            since = next_since
            if since > as_of:
                break
        frames[timeframe] = build_frame(rows, exchange_id, symbol, timeframe, "live", as_of)
    return frames


def demo_market(symbol, as_of=None):
    """Synthetic, internally consistent multi-timeframe data; never a live fallback."""
    as_of = int(time.time() * 1000) if as_of is None else as_of
    end = as_of // TIMEFRAMES["1h"] * TIMEFRAMES["1h"]
    start = (end // TIMEFRAMES["1d"] - 620) * TIMEFRAMES["1d"]
    timestamps = np.arange(start, end, TIMEFRAMES["1h"], dtype=np.int64)
    rng = np.random.default_rng(42 if symbol == "BTC/USDT" else 73)
    index = np.arange(len(timestamps))
    returns = rng.normal(0.000015, 0.003, len(index)) + np.sin(index / 70) * 0.00025
    close = (65000 if symbol == "BTC/USDT" else 2800) * np.exp(np.cumsum(returns))
    opening = np.r_[close[0], close[:-1]]
    spread = rng.uniform(0.0005, 0.003, len(index))
    df = pd.DataFrame({
        "open": opening, "high": np.maximum(opening, close) * (1 + spread),
        "low": np.minimum(opening, close) * (1 - spread), "close": close,
        "volume": rng.lognormal(4 if symbol == "BTC/USDT" else 6, 0.5, len(index)),
    }, index=pd.to_datetime(timestamps, unit="ms", utc=True))
    frames = {}
    for timeframe in TIMEFRAMES:
        grouped = df.resample("1D" if timeframe == "1d" else timeframe).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        rows = [[int(ts.timestamp() * 1000), *values] for ts, values in zip(grouped.index, grouped.to_numpy().tolist())]
        frames[timeframe] = build_frame(rows[-601:], "synthetic", symbol, timeframe, "synthetic", as_of)
    return frames
