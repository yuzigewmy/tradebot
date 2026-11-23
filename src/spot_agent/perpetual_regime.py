"""Select perpetual setups using completed OKX candles."""

from dataclasses import dataclass
from itertools import product

import numpy as np
import pandas as pd
import talib

from .perpetual_history import MINUTE

BREAKOUT_ATR_BUFFER = 0.03


@dataclass(frozen=True)
class Setup:
    family: str
    period: int
    lookback: int
    filtering: str
    min_volume: float


@dataclass(frozen=True)
class Brackets:
    stop_atr: float
    target_r: float
    hold: int
    trailing_atr: float = 0


@dataclass(frozen=True)
class Signal:
    at: int
    instrument: str
    strategy: str
    sign: int
    atr: float
    strength: float
    bracket: Brackets


def completed_bars(rows, period):
    step = period * MINUTE
    offset = (-int(rows[0, 0]) % step) // MINUTE
    count = (len(rows) - offset) // period
    groups = rows[offset:offset + count * period].reshape(count, period, 6)
    return np.column_stack((groups[:, 0, 0] + step, groups[:, 0, 1], groups[:, :, 2].max(1),
                            groups[:, :, 3].min(1), groups[:, -1, 4], groups[:, :, 5].sum(1)))


def shift(values, periods=1):
    return pd.Series(values).shift(periods).to_numpy()


def rolling(values, count, method):
    series = pd.Series(values).rolling(count)
    return getattr(series, method)().to_numpy()


def prior_channel(frame, lookback):
    return shift(rolling(frame["high"], lookback, "max")), shift(rolling(frame["low"], lookback, "min"))


def compression_range(frame):
    normalized_atr = frame["atr"] / frame["close"]
    return shift(normalized_atr), 0.9 * shift(rolling(normalized_atr, 60, "median"))


def indicators(bars):
    closed_at, opening, high, low, close, volume = bars.T
    ema = talib.EMA(close, 20)
    return {
        "times": closed_at, "opening": opening, "high": high, "low": low, "close": close,
        "ema": ema, "slow": talib.EMA(close, 50), "rsi": talib.RSI(close, 14),
        "adx": talib.ADX(high, low, close, 14), "atr": talib.ATR(high, low, close, 14),
        "volume_ratio": volume / shift(rolling(volume, 20, "mean")),
    }


class InstrumentFeatures:
    def __init__(self, rows):
        self.frames = {period: indicators(completed_bars(rows, period)) for period in (15, 30, 60, 240)}
        self.cache = {}

    def select(self, setup):
        if setup not in self.cache:
            self.cache[setup] = self._select(setup)
        return self.cache[setup]

    def _select(self, setup):
        f = self.frames[setup.period]
        t, high, low, close = (f[k] for k in ("times", "high", "low", "close"))
        atr, vol = f["atr"], f["volume_ratio"]
        prior_high, prior_low = prior_channel(f, setup.lookback)
        trend_bars = self.frames[60 if setup.period <= 30 else 240]
        ht = np.searchsorted(trend_bars["times"], t, side="right") - 1
        trend_ema, trend_slow, trend_adx = (trend_bars[k][ht] for k in ("ema", "slow", "adx"))
        trend_slope = shift(trend_bars["ema"], 3)[ht]
        adx_valid = (ht >= 0) & np.isfinite(trend_adx)
        if setup.filtering == "trend":
            regime_up = adx_valid & (trend_adx >= 20) & (trend_ema > trend_slow) & (trend_ema > trend_slope)
            regime_down = adx_valid & (trend_adx >= 20) & (trend_ema < trend_slow) & (trend_ema < trend_slope)
        elif setup.filtering == "range":
            regime_up = regime_down = adx_valid & (trend_adx < 23)
        elif setup.filtering == "macro":
            dominant = self.frames[240]
            last = np.searchsorted(dominant["times"], t, side="right") - 1
            momentum = (dominant["close"] / shift(dominant["close"], 18) - 1)[last]
            fast = dominant["ema"][last]
            slow = dominant["slow"][last]
            regime_up = adx_valid & (last >= 0) & (momentum > 0) & (fast > slow)
            regime_down = adx_valid & (last >= 0) & (momentum < 0) & (fast < slow)
        elif setup.filtering == "direction":
            regime_up = adx_valid & (trend_ema > trend_slow)
            regime_down = adx_valid & (trend_ema < trend_slow)
        else:
            regime_up = regime_down = adx_valid
        if setup.family in ("channel", "compression"):
            longs = close > prior_high + BREAKOUT_ATR_BUFFER * atr
            shorts = close < prior_low - BREAKOUT_ATR_BUFFER * atr
            if setup.family == "compression":
                relative, threshold = compression_range(f)
                quiet = relative < threshold
                longs &= quiet
                shorts &= quiet
        elif setup.family == "pullback":
            longs = (low <= f["ema"]) & (close > f["ema"]) & (close > shift(high))
            shorts = (high >= f["ema"]) & (close < f["ema"]) & (close < shift(low))
        elif setup.family == "band_reversion":
            upper, _, lower = talib.BBANDS(close, setup.lookback, 2, 2)
            longs = (shift(close) < shift(lower)) & (close > lower) & (shift(f["rsi"]) < 38)
            shorts = (shift(close) > shift(upper)) & (close < upper) & (shift(f["rsi"]) > 62)
        elif setup.family == "dual_momentum":
            fast = talib.EMA(close, setup.lookback)
            slow = talib.EMA(close, setup.lookback * 3)
            direction = np.sign(fast - slow)
            return_sign = np.sign(close - shift(close, setup.lookback))
            agree = np.where(direction == return_sign, direction, 0)
            longs = (agree == 1) & ((shift(agree) != 1) | ((shift(close) <= shift(fast)) & (close > fast)))
            shorts = (agree == -1) & ((shift(agree) != -1) | ((shift(close) >= shift(fast)) & (close < fast)))
        elif setup.family == "impulse":
            recent = (close - shift(close, setup.lookback)) / (atr * np.sqrt(setup.lookback))
            acceleration = (close - shift(close, 3)) / (atr * np.sqrt(3))
            longs = (recent > 1.0) & (acceleration > 0.4) & (close > shift(high)) & (shift(recent) <= 1.0)
            shorts = (recent < -1.0) & (acceleration < -0.4) & (close < shift(low)) & (shift(recent) >= -1.0)
        elif setup.family == "opening_range":
            step = setup.period * MINUTE
            cycle = 480 * MINUTE
            slot = (t.astype(np.int64) - step) // cycle
            phase = (t.astype(np.int64) - step) % cycle // step
            prior_high_open = pd.Series(np.where(phase < setup.lookback, high, np.nan)).groupby(slot).cummax().groupby(slot).ffill().to_numpy()
            prior_low_open = pd.Series(np.where(phase < setup.lookback, low, np.nan)).groupby(slot).cummin().groupby(slot).ffill().to_numpy()
            window = (phase >= setup.lookback) & (phase < setup.lookback + 240 // setup.period)
            longs = window & (close > prior_high_open + 0.1 * atr) & (shift(close) <= prior_high_open)
            shorts = window & (close < prior_low_open - 0.1 * atr) & (shift(close) >= prior_low_open)
        elif setup.family == "failed_breakout":
            longs = (low < prior_low - 0.15 * atr) & (close > prior_low) & (close > f["opening"])
            shorts = (high > prior_high + 0.15 * atr) & (close < prior_high) & (close < f["opening"])
        else:
            raise ValueError(setup.family)
        volume = np.isfinite(atr) & (atr > 0) & (vol >= setup.min_volume)
        picked = np.where(volume & regime_up & longs, 1, np.where(volume & regime_down & shorts, -1, 0))
        indices = np.flatnonzero(picked)
        return [(int(t[i]), int(picked[i]), float(atr[i]), float(vol[i])) for i in indices]


STRATEGY_SETS = {
    "channel": [Setup("channel", timeframe, lookback, regime, volume)
                for timeframe, lookback, regime, volume in product((15, 60, 240), (12, 24, 48), ("trend", "direction", "any"), (1.05, 1.35))],
    "compression": [Setup("compression", timeframe, lookback, regime, 1.25)
                    for timeframe, lookback, regime in product((15, 30, 60), (12, 24), ("trend", "any"))],
    "pullback": [Setup("pullback", timeframe, 20, "trend", volume)
                 for timeframe, volume in product((15, 30, 60, 240), (1.05, 1.3))],
    "band_reversion": [Setup("band_reversion", timeframe, period, regime, 0.95)
                       for timeframe, period, regime in product((15, 30, 60), (20, 32), ("range", "any"))],
    "failed_breakout": [Setup("failed_breakout", timeframe, lookback, regime, volume)
                        for timeframe, lookback, regime, volume in product((15, 30, 60), (16, 32), ("range", "any"), (1.15, 1.5))],
}

BRACKETS = {
    "channel": (Brackets(1.5, 2.5, 240), Brackets(1.8, 3, 480), Brackets(2.0, 4, 960, 2.8), Brackets(2, 5, 1440, 3.2)),
    "compression": (Brackets(1.5, 2.5, 240), Brackets(1.8, 3, 480), Brackets(2, 4, 960, 2.8)),
    "pullback": (Brackets(1.5, 2, 240), Brackets(1.8, 3, 480), Brackets(2, 4, 960, 2.8)),
    "band_reversion": (Brackets(1.4, 1.5, 120), Brackets(1.8, 2, 240), Brackets(2.2, 2.5, 480)),
    "failed_breakout": (Brackets(1.4, 1.75, 120), Brackets(1.8, 2.25, 240), Brackets(2.2, 3, 480)),
}

PHASE_TWO = {
    "macro_channel": [Setup("channel", timeframe, lookback, "macro", volume)
                      for timeframe, lookback, volume in product((15, 30, 60, 240), (12, 24, 48), (1.0, 1.25))],
    "macro_compression": [Setup("compression", timeframe, lookback, regime, volume)
                          for timeframe, lookback, regime, volume in product((15, 30, 60), (12, 24, 48), ("trend", "macro"), (1.15, 1.4))],
    "impulse": [Setup("impulse", timeframe, lookback, regime, volume)
                for timeframe, lookback, regime, volume in product((15, 30, 60, 240), (8, 16, 24), ("trend", "macro", "any"), (1.0, 1.3))],
    "opening_range": [Setup("opening_range", timeframe, lookback, regime, volume)
                      for timeframe, lookback, regime, volume in product((15, 30, 60), (2, 4), ("trend", "macro", "any"), (1.0, 1.3))],
}

SLOW_TRENDS = [Setup("dual_momentum", period, lookback, regime, volume)
               for period, lookback, regime, volume in product((60, 240), (8, 12, 20), ("macro", "direction", "any"), (0.5, 0.9))]
SLOW_BRACKETS = (Brackets(2, 3, 480, 2.8), Brackets(2, 3, 1440, 2.8), Brackets(2.4, 3.5, 2880, 3.0), Brackets(2.6, 5, 4320, 3.5))
