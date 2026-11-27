"""Automatic Regime Router (Auto Switch A): Strong Trend -> S10 (8% risk x 30x), Chop -> A1 Aggressive (8% risk x 15x)."""

from decimal import Decimal
import hashlib
import json

import numpy as np

from .contracts import TIMEFRAMES
from .perpetual_dual import DUAL_COOLDOWN_MINUTES, DUAL_MAX_STOP_PCT, DUAL_MIN_STOP_PCT, DUAL_STRATEGY, dual_engine_setup
from .perpetual_history import MINUTE, SECOND
from .perpetual_macro import MACRO_A1_AGGRESSIVE, MACRO_PROFILES, macro_a1_setup, macro_a1_signal_candidate
from .perpetual_paper import FEE_RATE, SLIPPAGE, PaperAccount, d
from .perpetual_regime import Brackets, indicators
from .perpetual_regime_terms import CAPITAL, ENTRY_ISSUES, FIXED_ENTRY_WINDOW, FixedSignalPolicy, adverse_tick

AUTO_REGIME_STRATEGY = "auto_regime_router"
AUTO_REGIME_VERSION = "perpetual-auto-regime-a/v1"

ROUTER_D1_ADX_MIN = 25.0
ROUTER_H4_ADX_MIN = 22.0

ROUTER_S10_RISK_PERCENT = Decimal("8")
ROUTER_S10_LEVERAGE = Decimal("30")
ROUTER_S10_MAX_NOTIONAL = Decimal("15")

AUTO_REGIME_POLICY_ID = hashlib.sha256(json.dumps({
    "version": AUTO_REGIME_VERSION,
    "d1_adx_min": ROUTER_D1_ADX_MIN,
    "h4_adx_min": ROUTER_H4_ADX_MIN,
    "trend_engine": {
        "strategy": DUAL_STRATEGY, "risk_percent": str(ROUTER_S10_RISK_PERCENT),
        "leverage": str(ROUTER_S10_LEVERAGE), "max_notional_multiple": str(ROUTER_S10_MAX_NOTIONAL),
        "stop_range": [DUAL_MIN_STOP_PCT, DUAL_MAX_STOP_PCT], "cooldown_minutes": DUAL_COOLDOWN_MINUTES,
    },
    "chop_engine": {
        "strategy": MACRO_A1_AGGRESSIVE,
        "risk_percent": str(MACRO_PROFILES[MACRO_A1_AGGRESSIVE].risk_percent),
        "leverage": str(MACRO_PROFILES[MACRO_A1_AGGRESSIVE].leverage),
        "max_notional_multiple": str(MACRO_PROFILES[MACRO_A1_AGGRESSIVE].max_notional_multiple),
        "max_stop_pct": MACRO_PROFILES[MACRO_A1_AGGRESSIVE].max_stop_pct,
    },
    "entry_window": FIXED_ENTRY_WINDOW,
}, sort_keys=True).encode()).hexdigest()[:12]

AUTO_REGIME_POLICY = FixedSignalPolicy(
    AUTO_REGIME_STRATEGY, "ALL", AUTO_REGIME_VERSION, AUTO_REGIME_POLICY_ID,
    Brackets(2.0, 2.5, 48 * 60, 0.0),
)


def _frame(rows, timeframe):
    bars = np.asarray(rows, dtype=float).copy()
    bars[:, 0] += TIMEFRAMES[timeframe]
    return indicators(bars)


def detect_regime(windows):
    """Classify closed 1D + 4H market state into strong trend (S10) or chop/mixed (A1)."""
    h4 = _frame(windows["4h"], "4h")
    d1 = _frame(windows["1d"], "1d")
    h4_closed = int(h4["times"][-1])
    d_idx = int(np.searchsorted(d1["times"], h4_closed, side="right") - 1)

    d_valid = (d_idx >= 0 and np.isfinite(d1["ema"][d_idx]) and np.isfinite(d1["slow"][d_idx])
               and np.isfinite(d1["close"][d_idx]) and np.isfinite(d1["adx"][d_idx]))
    if d_valid and float(d1["ema"][d_idx]) > float(d1["slow"][d_idx]) and float(d1["close"][d_idx]) > float(d1["slow"][d_idx]):
        d_dir = 1
    elif d_valid and float(d1["ema"][d_idx]) < float(d1["slow"][d_idx]) and float(d1["close"][d_idx]) < float(d1["slow"][d_idx]):
        d_dir = -1
    else:
        d_dir = 0
    d_adx = float(d1["adx"][d_idx]) if d_valid else 0.0

    h4_valid = np.isfinite(h4["ema"][-1]) and np.isfinite(h4["slow"][-1]) and np.isfinite(h4["adx"][-1])
    h4_dir = (1 if float(h4["ema"][-1]) > float(h4["slow"][-1]) else -1) if h4_valid else 0
    h4_adx = float(h4["adx"][-1]) if h4_valid else 0.0

    aligned = d_dir != 0 and d_dir == h4_dir
    is_trend = aligned and d_adx >= ROUTER_D1_ADX_MIN and h4_adx >= ROUTER_H4_ADX_MIN
    a1_profile = MACRO_PROFILES[MACRO_A1_AGGRESSIVE]

    return {
        "state": "trend" if is_trend else "chop",
        "label": "强单边 · 路由 S10" if is_trend else "震荡盘 · 路由 A1 激进",
        "routed_strategy": DUAL_STRATEGY if is_trend else MACRO_A1_AGGRESSIVE,
        "d1_side": "long" if d_dir == 1 else "short" if d_dir == -1 else "mixed",
        "h4_side": "long" if h4_dir == 1 else "short" if h4_dir == -1 else "mixed",
        "aligned": bool(aligned),
        "d1_adx": round(d_adx, 2),
        "h4_adx": round(h4_adx, 2),
        "active_leverage": int(ROUTER_S10_LEVERAGE if is_trend else a1_profile.leverage),
        "active_risk_percent": float(ROUTER_S10_RISK_PERCENT if is_trend else a1_profile.risk_percent),
        "active_max_notional": float(ROUTER_S10_MAX_NOTIONAL if is_trend else a1_profile.max_notional_multiple),
        "evaluated_at": h4_closed,
    }


def auto_regime_setup(windows):
    """Evaluate active sub-strategy according to closed 1D/4H market regime."""
    regime = detect_regime(windows)
    if regime["state"] == "trend":
        sub = dual_engine_setup(windows)
        triggered = bool(sub["strategy"]) and sub["side"] == regime["d1_side"]
        return sub | {
            "strategy": AUTO_REGIME_STRATEGY if triggered else None,
            "routed_strategy": DUAL_STRATEGY,
            "cooldown_minutes": DUAL_COOLDOWN_MINUTES,
            "regime": regime,
        }

    sub = macro_a1_setup(windows, MACRO_A1_AGGRESSIVE)
    triggered = bool(sub["strategy"])
    return sub | {
        "strategy": AUTO_REGIME_STRATEGY if triggered else None,
        "routed_strategy": MACRO_A1_AGGRESSIVE,
        "regime": regime,
    }


def auto_regime_signal_candidate(instrument, setup, market_quote, rules, now, *, binance=False):
    routed = setup["routed_strategy"]
    if routed == MACRO_A1_AGGRESSIVE:
        cand, issue = macro_a1_signal_candidate(
            instrument, MACRO_A1_AGGRESSIVE, setup, market_quote, rules, now, binance=binance
        )
        if cand is None:
            return None, issue
        return cand | {
            "strategy": AUTO_REGIME_STRATEGY, "routed_strategy": MACRO_A1_AGGRESSIVE,
            "regime_state": "chop", "policy_id": AUTO_REGIME_POLICY_ID, "policy_version": AUTO_REGIME_VERSION,
        }, None

    # Trend regime -> S10 with 8% risk x 30x isolated (max 15x notional, 2.4% stop cap)
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
        "side": side, "strategy": AUTO_REGIME_STRATEGY, "routed_strategy": DUAL_STRATEGY,
        "regime_state": "trend", "engine": setup["engine"], "closed_at": closed_at,
        "entry_at": entry_at, "expires_at": expires_at, "exit_at": exit_at,
        "entry": float(entry), "stop_loss": float(stop), "take_profit": float(target),
        "atr": float(setup["atr"]), "breakeven_r": float(setup["breakeven_r"]),
        "trailing_atr": float(setup["trailing_atr"]),
        "risk_per_unit": float(risk), "net_reward_risk": round(float(reward / risk), 4),
        "funding_cost_rate": 0.0, "policy_id": AUTO_REGIME_POLICY_ID, "policy_version": AUTO_REGIME_VERSION,
        "entry_reference": f"best_{entry_side}", "exit_reference": "last_trade",
        "risk_percent": float(ROUTER_S10_RISK_PERCENT), "leverage": int(ROUTER_S10_LEVERAGE),
        "max_notional_multiple": float(ROUTER_S10_MAX_NOTIONAL),
        "cooldown_minutes": DUAL_COOLDOWN_MINUTES,
    }
    if binance:
        if not min(stop, target) < d(market_quote["last"]) < max(stop, target):
            return None, "币安最新成交价已越过退出价位"
        return candidate, None

    account = PaperAccount({instrument: rules}, cash=CAPITAL, leverage=ROUTER_S10_LEVERAGE,
                           risk=ROUTER_S10_RISK_PERCENT / Decimal("100"))
    state = account.enter(instrument, candidate, market_quote | {entry_side: entry}, now)
    if state != "entered":
        return None, ENTRY_ISSUES[state]
    position = account.positions[instrument]
    return candidate | {"contracts": float(position.contracts)}, None
