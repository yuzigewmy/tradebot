"""Frozen ETH volatility compression rules from the November research replay."""

from dataclasses import asdict
import hashlib
import json

import numpy as np

from .perpetual_history import MINUTE
from .perpetual_paper import FEE_RATE, SLIPPAGE
from .perpetual_regime import BREAKOUT_ATR_BUFFER, Brackets, Setup, compression_range, indicators, prior_channel
from .perpetual_regime_terms import (FIXED_COOLDOWN_MINUTES, FIXED_ENTRY_WINDOW, FIXED_FUNDING_BUFFER,
                                     LEVERAGE, MARGIN, MAX_STOP_PCT, MIN_STOP_PCT, FixedSignalPolicy)

COMPRESSION_STRATEGY = "eth_compression_short"
COMPRESSION_INSTRUMENT = "ETH-USDT-SWAP"
COMPRESSION_SETUP = Setup("compression", 30, 24, "any", 1.25)
COMPRESSION_BRACKETS = Brackets(1.5, 2.5, 240)
COMPRESSION_VERSION = "perpetual-compression/v1"
COMPRESSION_POLICY_ID = hashlib.sha256(json.dumps({
    "version": COMPRESSION_VERSION, "setup": asdict(COMPRESSION_SETUP), "brackets": asdict(COMPRESSION_BRACKETS),
    "funding_buffer": FIXED_FUNDING_BUFFER, "cooldown": FIXED_COOLDOWN_MINUTES * MINUTE, "margin": str(MARGIN),
    "leverage": str(LEVERAGE), "fee": str(FEE_RATE), "slippage": str(SLIPPAGE),
    "stop_range": [MIN_STOP_PCT, MAX_STOP_PCT], "breakout_buffer": BREAKOUT_ATR_BUFFER,
    "entry_window": FIXED_ENTRY_WINDOW,
}, sort_keys=True).encode()).hexdigest()[:12]

COMPRESSION_POLICY = FixedSignalPolicy(COMPRESSION_STRATEGY, COMPRESSION_INSTRUMENT, COMPRESSION_VERSION,
                                       COMPRESSION_POLICY_ID, COMPRESSION_BRACKETS)


def compression_setup(rows):
    bars = np.array(rows, dtype=float)
    bars[:, 0] += COMPRESSION_SETUP.period * MINUTE
    frame = indicators(bars)
    prior_high, prior_low = (float(v[-1]) for v in prior_channel(frame, COMPRESSION_SETUP.lookback))
    previous_atr, threshold = (float(v[-1]) for v in compression_range(frame))
    close, atr, volume_ratio = (float(frame[key][-1]) for key in ("close", "atr", "volume_ratio"))
    compressed = previous_atr < threshold
    crossed = close < prior_low - BREAKOUT_ATR_BUFFER * atr
    volume_pass = volume_ratio >= COMPRESSION_SETUP.min_volume
    return {"strategy": COMPRESSION_STRATEGY if compressed and crossed and volume_pass and atr > 0 else None,
            "side": "short", "closed_at": int(frame["times"][-1]), "close": close, "atr": atr,
            "compressed": compressed, "range_cross": crossed, "volume_pass": volume_pass, "volume_ratio": volume_ratio,
            "prior_high": prior_high, "prior_low": prior_low, "stop_anchor": prior_low,
            "previous_atr_ratio": previous_atr, "atr_ratio_threshold": threshold}
