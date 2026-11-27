"""S10 Dual-Engine Trend Strategy: 1H Pullback+RSI Reset (S4) + 1H Keltner Breakout (S3)."""

from dataclasses import asdict
from decimal import Decimal
import hashlib
import json

import numpy as np

from .contracts import TIMEFRAMES
from .perpetual_history import MINUTE, SECOND
from .perpetual_paper import FEE_RATE, SLIPPAGE, PaperAccount, d
from .perpetual_regime import Brackets, indicators, rolling, shift
from .perpetual_regime_terms import CAPITAL, ENTRY_ISSUES, FIXED_ENTRY_WINDOW, LEVERAGE, MARGIN, FixedSignalPolicy, adverse_tick

DUAL_STRATEGY = "dual_trend_engine"
DUAL_VERSION = "perpetual-dual-engine/v1"
DUAL_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")

DUAL_MARGIN_PERCENT = 25
DUAL_LEVERAGE = LEVERAGE
DUAL_COOLDOWN_MINUTES = 120
DUAL_MIN_STOP_PCT = 0.004
DUAL_MAX_STOP_PCT = 0.024

S4_BRACKETS = Brackets(2.0, 2.5, 48 * 60, 0.0)
S4_BREAKEVEN_R = 1.2
S3_BRACKETS = Brackets(2.0, 4.5, 48 * 60, 2.2)
S3_BREAKEVEN_R = 1.5

DUAL_POLICY_ID = hashlib.sha256(json.dumps({
    "version": DUAL_VERSION,
    "s4": {"brackets": asdict(S4_BRACKETS), "breakeven_r": S4_BREAKEVEN_R, "adx_min": 18, "rsi_band": [42, 45, 55, 58]},
    "s3": {"brackets": asdict(S3_BRACKETS), "breakeven_r": S3_BREAKEVEN_R, "adx_min": 20, "kc_mult": 2.0, "min_volume": 1.1},
    "margin_percent": DUAL_MARGIN_PERCENT, "leverage": str(DUAL_LEVERAGE),
    "stop_range": [DUAL_MIN_STOP_PCT, DUAL_MAX_STOP_PCT],
    "cooldown_minutes": DUAL_COOLDOWN_MINUTES, "entry_window": FIXED_ENTRY_WINDOW,
}, sort_keys=True).encode()).hexdigest()[:12]

DUAL_POLICY = FixedSignalPolicy(DUAL_STRATEGY, "ALL", DUAL_VERSION, DUAL_POLICY_ID, S4_BRACKETS)


def _frame(rows, timeframe):
    bars = np.asarray(rows, dtype=float).copy()
    bars[:, 0] += TIMEFRAMES[timeframe]
    return indicators(bars)


def dual_engine_setup(windows):
    """Evaluate S4 (1H Pullback + RSI Reset) and S3 (1H Keltner Breakout) on closed 4H/1H windows."""
    h4 = _frame(windows["4h"], "4h")
    h1 = _frame(windows["1h"], "1h")
    idx = int(np.searchsorted(h4["times"], h1["times"][-1], side="right") - 1)

    h4_valid = idx >= 0 and np.isfinite(h4["ema"][idx]) and np.isfinite(h4["slow"][idx]) and np.isfinite(h4["adx"][idx])
    h4_dir = (1 if h4["ema"][idx] > h4["slow"][idx] else -1) if h4_valid else 0
    h4_adx = float(h4["adx"][idx]) if h4_valid else 0.0

    close = h1["close"]
    ema = h1["ema"]
    atr = h1["atr"]
    rsi = h1["rsi"]
    vol_ratio = float(h1["volume_ratio"][-1]) if np.isfinite(h1["volume_ratio"][-1]) else 0.0
    cur_close, cur_ema, cur_atr, cur_rsi = (float(arr[-1]) for arr in (close, ema, atr, rsi))
    prev_high, prev_low = float(h1["high"][-2]), float(h1["low"][-2])
    closed_at = int(h1["times"][-1])
    atr_ok = np.isfinite(cur_atr) and cur_atr > 0

    # S4: 1H Trend Pullback + RSI Reset
    rsi_min3 = float(rolling(shift(rsi), 3, "min")[-1])
    rsi_max3 = float(rolling(shift(rsi), 3, "max")[-1])
    s4_trend = h4_valid and h4_adx >= 18.0
    if h4_dir == 1:
        s4_pullback = np.isfinite(rsi_min3) and rsi_min3 < 42.0
        s4_trigger = cur_close > cur_ema and cur_close > prev_high and cur_rsi > 45.0
    elif h4_dir == -1:
        s4_pullback = np.isfinite(rsi_max3) and rsi_max3 > 58.0
        s4_trigger = cur_close < cur_ema and cur_close < prev_low and cur_rsi < 55.0
    else:
        s4_pullback = s4_trigger = False
    s4_valid = s4_trend and s4_pullback and s4_trigger and atr_ok

    # S3: 1H Keltner Channel Breakout (EMA20 ± 2.0*ATR)
    kc_up = ema + 2.0 * atr
    kc_dn = ema - 2.0 * atr
    s3_trend = h4_valid and h4_adx >= 20.0
    s3_volume = vol_ratio >= 1.1
    if h4_dir == 1:
        s3_cross = float(close[-2]) <= float(kc_up[-2]) and cur_close > float(kc_up[-1])
    elif h4_dir == -1:
        s3_cross = float(close[-2]) >= float(kc_dn[-2]) and cur_close < float(kc_dn[-1])
    else:
        s3_cross = False
    s3_valid = s3_trend and s3_cross and s3_volume and atr_ok

    side = "long" if h4_dir == 1 else "short" if h4_dir == -1 else "mixed"
    engines = [
        {
            "engine": "s4_pullback", "label": "S4 顺势回踩", "timeframe": "1h", "side": side,
            "closed_at": closed_at, "trend_pass": bool(s4_trend), "pullback_pass": bool(s4_pullback),
            "trigger_pass": bool(s4_trigger), "rsi": round(cur_rsi, 2) if np.isfinite(cur_rsi) else None,
            "active": bool(s4_valid),
        },
        {
            "engine": "s3_keltner", "label": "S3 通道突破", "timeframe": "1h", "side": side,
            "closed_at": closed_at, "trend_pass": bool(s3_trend), "breakout_pass": bool(s3_cross),
            "volume_pass": bool(s3_volume), "volume_ratio": round(vol_ratio, 2),
            "kc_low": round(float(kc_dn[-1]), 4) if np.isfinite(kc_dn[-1]) else None,
            "kc_high": round(float(kc_up[-1]), 4) if np.isfinite(kc_up[-1]) else None,
            "active": bool(s3_valid),
        },
    ]

    # Prefer S4 when both trigger on the same 1H close (higher win rate)
    chosen = "s4_pullback" if s4_valid else "s3_keltner" if s3_valid else None
    brackets = S4_BRACKETS if chosen != "s3_keltner" else S3_BRACKETS
    breakeven_r = S4_BREAKEVEN_R if chosen != "s3_keltner" else S3_BREAKEVEN_R

    return {
        "strategy": DUAL_STRATEGY if chosen else None,
        "engine": chosen,
        "side": side,
        "timeframe": "1h",
        "closed_at": closed_at,
        "close": cur_close,
        "atr": cur_atr,
        "stop_anchor": cur_ema,
        "volume_ratio": vol_ratio,
        "h4_adx": round(h4_adx, 2),
        "breakeven_r": breakeven_r,
        "trailing_atr": brackets.trailing_atr,
        "target_r": brackets.target_r,
        "stop_atr": brackets.stop_atr,
        "hold_minutes": brackets.hold,
        "engines": engines,
    }


def dual_signal_candidate(instrument, setup, market_quote, rules, now, *, binance=False):
    closed_at = setup["closed_at"]
    entry_at = closed_at + MINUTE
    expires_at = entry_at + FIXED_ENTRY_WINDOW
    if not entry_at <= market_quote["ticker_at"] <= now or (now + SECOND - 1) // SECOND * SECOND >= expires_at:
        return None, "未取得入场时间窗内的新盘口"

    side = setup["side"]
    sign = 1 if side == "long" else -1
    entry_side = "ask" if sign == 1 else "bid"
    entry = adverse_tick(market_quote[entry_side], sign, rules.tick, SLIPPAGE)

    stop_dist = min(DUAL_MAX_STOP_PCT * float(entry), max(DUAL_MIN_STOP_PCT * float(entry), setup["stop_atr"] * setup["atr"]))
    stop = adverse_tick(entry - sign * d(stop_dist), -sign, rules.tick, 0)
    target = adverse_tick(entry + sign * d(stop_dist) * d(setup["target_r"]), sign, rules.tick, 0)
    stop_fill = adverse_tick(stop, -sign, rules.tick, SLIPPAGE)
    target_fill = adverse_tick(target, -sign, rules.tick, SLIPPAGE)
    risk = sign * (entry - stop_fill) + (entry + stop_fill) * FEE_RATE
    reward = sign * (target_fill - entry) - (entry + target_fill) * FEE_RATE
    exit_at = entry_at + setup["hold_minutes"] * MINUTE

    candidate = {
        "side": side, "strategy": DUAL_STRATEGY, "engine": setup["engine"], "closed_at": closed_at,
        "entry_at": entry_at, "expires_at": expires_at, "exit_at": exit_at,
        "entry": float(entry), "stop_loss": float(stop), "take_profit": float(target),
        "atr": float(setup["atr"]), "breakeven_r": float(setup["breakeven_r"]),
        "trailing_atr": float(setup["trailing_atr"]),
        "risk_per_unit": float(risk), "net_reward_risk": round(float(reward / risk), 4),
        "funding_cost_rate": 0.0, "policy_id": DUAL_POLICY_ID, "policy_version": DUAL_VERSION,
        "entry_reference": f"best_{entry_side}", "exit_reference": "last_trade",
        "margin_percent": DUAL_MARGIN_PERCENT, "leverage": int(DUAL_LEVERAGE),
        "cooldown_minutes": DUAL_COOLDOWN_MINUTES,
    }
    if binance:
        if not min(stop, target) < d(market_quote["last"]) < max(stop, target):
            return None, "币安最新成交价已越过退出价位"
        return candidate, None

    account = PaperAccount({instrument: rules}, cash=CAPITAL, leverage=DUAL_LEVERAGE, fixed_margin=MARGIN)
    state = account.enter(instrument, candidate, market_quote | {entry_side: entry}, now)
    if state != "entered":
        return None, ENTRY_ISSUES[state]
    position = account.positions[instrument]
    return candidate | {"contracts": float(position.contracts)}, None
