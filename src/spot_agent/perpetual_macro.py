"""A1 Macro Dual-Engine Strategy: 4H Supertrend(10, 3.0) + 1D EMA50 & 1D Donchian(14) CTA."""

from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json

import numpy as np
import talib

from .contracts import TIMEFRAMES
from .perpetual_history import MINUTE, SECOND
from .perpetual_paper import FEE_RATE, SLIPPAGE, PaperAccount, d
from .perpetual_regime import Brackets, indicators, rolling, shift
from .perpetual_regime_terms import CAPITAL, ENTRY_ISSUES, FIXED_ENTRY_WINDOW, FixedSignalPolicy, adverse_tick

MACRO_A1_AGGRESSIVE = "macro_a1_aggressive"
MACRO_A1_STEADY = "macro_a1_steady"
MACRO_VERSION = "perpetual-macro-a1/v1"

M3_BRACKETS = Brackets(2.8, 8.0, 240 * 60, 3.0)
M3_BREAKEVEN_R = 1.5
M3_COOLDOWN_MINUTES = 12 * 60

M5_BRACKETS = Brackets(2.0, 10.0, 720 * 60, 2.5)
M5_BREAKEVEN_R = 1.5
M5_COOLDOWN_MINUTES = 48 * 60


@dataclass(frozen=True)
class MacroProfile:
    strategy: str
    label: str
    leverage: Decimal
    risk_percent: Decimal
    max_notional_multiple: Decimal
    max_stop_pct: float
    min_stop_pct: float = 0.005


MACRO_PROFILES = {
    MACRO_A1_AGGRESSIVE: MacroProfile(
        MACRO_A1_AGGRESSIVE, "A1 宏观双引擎 · 激进版",
        Decimal("15"), Decimal("8"), Decimal("10"), 0.050,
    ),
    MACRO_A1_STEADY: MacroProfile(
        MACRO_A1_STEADY, "A1 宏观双引擎 · 稳健版",
        Decimal("10"), Decimal("5"), Decimal("7"), 0.075,
    ),
}
MACRO_STRATEGIES = tuple(MACRO_PROFILES)


def _policy_id(profile):
    return hashlib.sha256(json.dumps({
        "version": MACRO_VERSION, "strategy": profile.strategy,
        "m3": {"st_period": 10, "st_mult": 3.0, "brackets": asdict(M3_BRACKETS), "breakeven_r": M3_BREAKEVEN_R, "cooldown": M3_COOLDOWN_MINUTES},
        "m5": {"donchian": 14, "brackets": asdict(M5_BRACKETS), "breakeven_r": M5_BREAKEVEN_R, "cooldown": M5_COOLDOWN_MINUTES},
        "leverage": str(profile.leverage), "risk_percent": str(profile.risk_percent),
        "max_notional_multiple": str(profile.max_notional_multiple),
        "stop_range": [profile.min_stop_pct, profile.max_stop_pct],
        "entry_window": FIXED_ENTRY_WINDOW,
    }, sort_keys=True).encode()).hexdigest()[:12]


MACRO_POLICIES = {
    key: FixedSignalPolicy(key, "ALL", MACRO_VERSION, _policy_id(profile), M3_BRACKETS)
    for key, profile in MACRO_PROFILES.items()
}


def _frame(rows, timeframe):
    bars = np.asarray(rows, dtype=float).copy()
    bars[:, 0] += TIMEFRAMES[timeframe]
    return indicators(bars)


def supertrend_direction(high, low, close, period=10, mult=3.0):
    atr = talib.ATR(high, low, close, period)
    hl2 = (high + low) / 2.0
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    direction = np.zeros(len(close), dtype=int)
    for i in range(1, len(close)):
        if not np.isfinite(atr[i]):
            continue
        if np.isfinite(upper[i - 1]) and not (upper[i] < upper[i - 1] or close[i - 1] > upper[i - 1]):
            upper[i] = upper[i - 1]
        if np.isfinite(lower[i - 1]) and not (lower[i] > lower[i - 1] or close[i - 1] < lower[i - 1]):
            lower[i] = lower[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < lower[i] else 1
        elif direction[i - 1] == -1:
            direction[i] = 1 if close[i] > upper[i] else -1
        else:
            direction[i] = 1 if close[i] > upper[i] else -1
    return direction


def macro_a1_setup(windows, strategy):
    """Evaluate M3 (4H Supertrend + 1D EMA50) and M5 (1D Donchian 14 CTA) on closed 4H/1D windows."""
    h4 = _frame(windows["4h"], "4h")
    d1 = _frame(windows["1d"], "1d")

    h4_closed = int(h4["times"][-1])
    d1_closed = int(d1["times"][-1])
    d_idx = int(np.searchsorted(d1["times"], h4_closed, side="right") - 1)
    d_valid = d_idx >= 0 and np.isfinite(d1["slow"][d_idx]) and np.isfinite(d1["close"][d_idx])
    d_dir = (1 if float(d1["close"][d_idx]) > float(d1["slow"][d_idx]) else -1) if d_valid else 0

    # M3: 4H Supertrend(10, 3.0) flip aligned with 1D close vs EMA50
    st_dir = supertrend_direction(h4["high"], h4["low"], h4["close"], period=10, mult=3.0)
    st_cur, st_prev = int(st_dir[-1]), int(st_dir[-2])
    h4_atr = float(h4["atr"][-1])
    h4_atr_ok = np.isfinite(h4_atr) and h4_atr > 0
    m3_flip = (d_dir == 1 and st_cur == 1 and st_prev != 1) or (d_dir == -1 and st_cur == -1 and st_prev != -1)
    m3_valid = d_valid and m3_flip and h4_atr_ok

    # M5: 1D Donchian(14) breakout aligned with 1D EMA50 (only on exact 1D bar close)
    d_hi14 = rolling(shift(d1["high"]), 14, "max")
    d_lo14 = rolling(shift(d1["low"]), 14, "min")
    d_close_cur, d_close_prev = float(d1["close"][-1]), float(d1["close"][-2])
    d_hi_cur, d_hi_prev = float(d_hi14[-1]), float(d_hi14[-2])
    d_lo_cur, d_lo_prev = float(d_lo14[-1]), float(d_lo14[-2])
    d1_atr = float(d1["atr"][-1])
    d1_atr_ok = np.isfinite(d1_atr) and d1_atr > 0
    on_daily_close = h4_closed == d1_closed
    if d_dir == 1 and np.isfinite(d_hi_cur) and np.isfinite(d_hi_prev):
        m5_cross = d_close_prev <= d_hi_prev and d_close_cur > d_hi_cur
    elif d_dir == -1 and np.isfinite(d_lo_cur) and np.isfinite(d_lo_prev):
        m5_cross = d_close_prev >= d_lo_prev and d_close_cur < d_lo_cur
    else:
        m5_cross = False
    m5_valid = d_valid and on_daily_close and m5_cross and d1_atr_ok

    side = "long" if d_dir == 1 else "short" if d_dir == -1 else "mixed"
    engines = [
        {
            "engine": "m3_4h_supertrend", "label": "M3 4H超趋势", "timeframe": "4h", "side": side,
            "closed_at": h4_closed, "trend_pass": bool(d_valid and d_dir != 0),
            "flip_pass": bool(m3_flip), "st_dir": st_cur, "active": bool(m3_valid),
        },
        {
            "engine": "m5_1d_donchian", "label": "M5 1D唐奇安CTA", "timeframe": "1d", "side": side,
            "closed_at": d1_closed, "trend_pass": bool(d_valid and d_dir != 0),
            "breakout_pass": bool(m5_cross),
            "don_low": round(d_lo_cur, 4) if np.isfinite(d_lo_cur) else None,
            "don_high": round(d_hi_cur, 4) if np.isfinite(d_hi_cur) else None,
            "active": bool(m5_valid),
        },
    ]

    chosen = "m3_4h_supertrend" if m3_valid else "m5_1d_donchian" if m5_valid else None
    use_m5 = chosen == "m5_1d_donchian"
    brackets = M5_BRACKETS if use_m5 else M3_BRACKETS
    breakeven_r = M5_BREAKEVEN_R if use_m5 else M3_BREAKEVEN_R
    cooldown = M5_COOLDOWN_MINUTES if use_m5 else M3_COOLDOWN_MINUTES
    timeframe = "1d" if use_m5 else "4h"
    closed_at = d1_closed if use_m5 else h4_closed
    atr = d1_atr if use_m5 else h4_atr
    cur_close = d_close_cur if use_m5 else float(h4["close"][-1])

    return {
        "strategy": strategy if chosen else None,
        "engine": chosen,
        "side": side,
        "timeframe": timeframe,
        "closed_at": closed_at,
        "close": cur_close,
        "atr": atr,
        "breakeven_r": breakeven_r,
        "trailing_atr": brackets.trailing_atr,
        "target_r": brackets.target_r,
        "stop_atr": brackets.stop_atr,
        "hold_minutes": brackets.hold,
        "cooldown_minutes": cooldown,
        "engines": engines,
    }


def macro_a1_signal_candidate(instrument, strategy, setup, market_quote, rules, now, *, binance=False):
    profile = MACRO_PROFILES[strategy]
    policy = MACRO_POLICIES[strategy]
    closed_at = setup["closed_at"]
    entry_at = closed_at + MINUTE
    expires_at = entry_at + FIXED_ENTRY_WINDOW
    if not entry_at <= market_quote["ticker_at"] <= now or (now + SECOND - 1) // SECOND * SECOND >= expires_at:
        return None, "未取得入场时间窗内的新盘口"

    side = setup["side"]
    sign = 1 if side == "long" else -1
    entry_side = "ask" if sign == 1 else "bid"
    entry = adverse_tick(market_quote[entry_side], sign, rules.tick, SLIPPAGE)

    stop_dist = min(profile.max_stop_pct * float(entry), max(profile.min_stop_pct * float(entry), setup["stop_atr"] * setup["atr"]))
    stop = adverse_tick(entry - sign * d(stop_dist), -sign, rules.tick, 0)
    target = adverse_tick(entry + sign * d(stop_dist) * d(setup["target_r"]), sign, rules.tick, 0)
    stop_fill = adverse_tick(stop, -sign, rules.tick, SLIPPAGE)
    target_fill = adverse_tick(target, -sign, rules.tick, SLIPPAGE)
    risk = sign * (entry - stop_fill) + (entry + stop_fill) * FEE_RATE
    reward = sign * (target_fill - entry) - (entry + target_fill) * FEE_RATE
    exit_at = entry_at + setup["hold_minutes"] * MINUTE

    candidate = {
        "side": side, "strategy": strategy, "engine": setup["engine"], "closed_at": closed_at,
        "entry_at": entry_at, "expires_at": expires_at, "exit_at": exit_at,
        "entry": float(entry), "stop_loss": float(stop), "take_profit": float(target),
        "atr": float(setup["atr"]), "breakeven_r": float(setup["breakeven_r"]),
        "trailing_atr": float(setup["trailing_atr"]),
        "risk_per_unit": float(risk), "net_reward_risk": round(float(reward / risk), 4),
        "funding_cost_rate": 0.0, "policy_id": policy.policy_id, "policy_version": MACRO_VERSION,
        "entry_reference": f"best_{entry_side}", "exit_reference": "last_trade",
        "risk_percent": float(profile.risk_percent), "leverage": int(profile.leverage),
        "max_notional_multiple": float(profile.max_notional_multiple),
        "cooldown_minutes": setup["cooldown_minutes"],
    }
    if binance:
        if not min(stop, target) < d(market_quote["last"]) < max(stop, target):
            return None, "币安最新成交价已越过退出价位"
        return candidate, None

    account = PaperAccount({instrument: rules}, cash=CAPITAL, leverage=profile.leverage,
                           risk=profile.risk_percent / Decimal("100"))
    state = account.enter(instrument, candidate, market_quote | {entry_side: entry}, now)
    if state != "entered":
        return None, ENTRY_ISSUES[state]
    position = account.positions[instrument]
    return candidate | {"contracts": float(position.contracts)}, None
