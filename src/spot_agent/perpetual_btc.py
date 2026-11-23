"""BTC higher-timeframe trend and EMA reclaim rules shared by live and replay."""

from dataclasses import asdict
import hashlib
import json

import numpy as np
import talib

from .contracts import TIMEFRAMES
from .perpetual_paper import FEE_RATE, SLIPPAGE
from .perpetual_regime import Brackets, Signal, indicators, shift
from .perpetual_regime_terms import (FIXED_COOLDOWN_MINUTES, FIXED_ENTRY_WINDOW, FIXED_FUNDING_BUFFER,
                                     LEVERAGE, MARGIN, MAX_STOP_PCT, MIN_STOP_PCT, FixedSignalPolicy)

BTC_INSTRUMENT = "BTC-USDT-SWAP"
BTC_STRATEGY = "btc_trend_reclaim"
BTC_VERSION = "perpetual-btc-trend/v1"
BTC_BRACKETS = Brackets(2, 4, 360)
BTC_LEGS = (("1h", 1, 1.0), ("30m", -1, 1.4))
BTC_EMA_PERIOD = 24
BTC_MOMENTUM_BARS = 12
BTC_POLICY_ID = hashlib.sha256(json.dumps({
    "version": BTC_VERSION, "legs": BTC_LEGS, "brackets": asdict(BTC_BRACKETS),
    "ema": BTC_EMA_PERIOD, "momentum_bars": BTC_MOMENTUM_BARS,
    "margin": str(MARGIN), "leverage": str(LEVERAGE), "fee": str(FEE_RATE), "slippage": str(SLIPPAGE),
    "stop_range": [MIN_STOP_PCT, MAX_STOP_PCT], "cooldown_minutes": FIXED_COOLDOWN_MINUTES,
    "funding_buffer": FIXED_FUNDING_BUFFER, "entry_window": FIXED_ENTRY_WINDOW,
}, sort_keys=True).encode()).hexdigest()[:12]
BTC_POLICY = FixedSignalPolicy(BTC_STRATEGY, BTC_INSTRUMENT, BTC_VERSION, BTC_POLICY_ID, BTC_BRACKETS)


def trend_leg(frame, four_hour, sign, volume_floor):
    indices = np.searchsorted(four_hour["times"], frame["times"], side="right") - 1
    close = frame["close"]
    basis = talib.EMA(close, BTC_EMA_PERIOD)
    if sign == 1:
        momentum = four_hour["close"] - shift(four_hour["close"], BTC_MOMENTUM_BARS)
        trend = ((momentum[indices] > 0) & (four_hour["close"][indices] > four_hour["ema"][indices])
                 & (four_hour["ema"][indices] > four_hour["slow"][indices]))
        crosses = (shift(close) <= shift(basis)) & (close > basis) & (close > shift(frame["high"]))
    else:
        trend = four_hour["ema"][indices] < four_hour["slow"][indices]
        crosses = (shift(close) >= shift(basis)) & (close < basis) & (close < shift(frame["low"]))
    trend &= indices >= 0
    volume_ok = frame["volume_ratio"] >= volume_floor
    valid = trend & crosses & volume_ok & (frame["atr"] > 0)
    return basis, trend, crosses, volume_ok, valid


def btc_signals(market):
    frames = market.features[BTC_INSTRUMENT].frames
    signals = []
    for timeframe, sign, floor in BTC_LEGS:
        frame = frames[TIMEFRAMES[timeframe] // TIMEFRAMES["1m"]]
        _, _, _, _, valid = trend_leg(frame, frames[240], sign, floor)
        for index in np.flatnonzero(valid):
            at = int(frame["times"][index])
            if market.start <= at and at + TIMEFRAMES["1m"] < market.end:
                signals.append(Signal(at, BTC_INSTRUMENT, BTC_STRATEGY, sign, float(frame["atr"][index]),
                                      float(frame["volume_ratio"][index]), BTC_BRACKETS))
    return signals


def live_btc_setup(windows):
    def frame_for(timeframe):
        bars = np.asarray(windows[timeframe], dtype=float).copy()
        bars[:, 0] += TIMEFRAMES[timeframe]
        return indicators(bars)

    four_hour = frame_for("4h")
    evidence = []
    for timeframe, sign, floor in BTC_LEGS:
        frame = frame_for(timeframe)
        basis, trend, crosses, volume_ok, valid = trend_leg(frame, four_hour, sign, floor)
        evidence.append({
            "timeframe": timeframe, "side": "long" if sign == 1 else "short",
            "closed_at": int(frame["times"][-1]), "atr": float(frame["atr"][-1]),
            "stop_anchor": float(basis[-1]), "volume_ratio": float(frame["volume_ratio"][-1]),
            "trend_pass": bool(trend[-1]), "range_cross": bool(crosses[-1]), "volume_pass": bool(volume_ok[-1]),
            "strategy": BTC_STRATEGY if valid[-1] else None,
        })
    latest = max(row["closed_at"] for row in evidence)
    selected = max((row for row in evidence if row["closed_at"] == latest),
                   key=lambda row: (row["strategy"] is not None, row["volume_ratio"]))
    return selected | {"legs": evidence}
