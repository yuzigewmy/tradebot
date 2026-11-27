"""Dedicated Per-Coin Strategies (D3 & D6): tailored independently to BTC, ETH, and SOL volatility regimes."""

from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json

import numpy as np
import talib

from .contracts import TIMEFRAMES
from .perpetual_history import MINUTE, SECOND
from .perpetual_macro import M5_BRACKETS, M5_BREAKEVEN_R, M5_COOLDOWN_MINUTES, supertrend_direction
from .perpetual_paper import FEE_RATE, SLIPPAGE, PaperAccount, d
from .perpetual_regime import Brackets, indicators, rolling, shift
from .perpetual_regime_terms import CAPITAL, ENTRY_ISSUES, FIXED_ENTRY_WINDOW, FixedSignalPolicy, adverse_tick

BTC_SQUEEZE_DON14 = "btc_squeeze_don14"   # D3 BTC: 4H TTM Squeeze + 1D Donchian(14)
BTC_DAILY_DON20 = "btc_daily_don20"       # D6 BTC: 1D Donchian(20) Turtle CTA
ETH_WIDE_ST_DON14 = "eth_wide_st_don14"   # D3/D6 ETH: 4H Wide Supertrend(14,3.8) + 1D Donchian(14)
SOL_DAILY_ST30 = "sol_daily_st30"         # D3/D6 SOL: 1D Wide Supertrend(10,3.0)

DEDICATED_VERSION = "perpetual-dedicated/v1"

SQ4H_BRACKETS = Brackets(2.2, 8.0, 168 * 60, 2.8)
SQ4H_BREAKEVEN_R = 1.5
SQ4H_COOLDOWN_MINUTES = 12 * 60

DON20_BRACKETS = Brackets(2.2, 12.0, 960 * 60, 2.8)
DON20_BREAKEVEN_R = 1.5
DON20_COOLDOWN_MINUTES = 48 * 60

ST14_BRACKETS = Brackets(3.2, 9.0, 336 * 60, 3.5)
ST14_BREAKEVEN_R = 1.6
ST14_COOLDOWN_MINUTES = 16 * 60

ST1D30_BRACKETS = Brackets(2.5, 12.0, 960 * 60, 2.8)
ST1D30_BREAKEVEN_R = 1.5
ST1D30_COOLDOWN_MINUTES = 24 * 60


@dataclass(frozen=True)
class DedicatedProfile:
    strategy: str
    instrument: str
    label: str
    detail: str
    leverage: Decimal
    risk_percent: Decimal
    max_notional_multiple: Decimal
    max_stop_pct: float
    min_stop_pct: float = 0.005


DEDICATED_PROFILES = {
    BTC_SQUEEZE_DON14: DedicatedProfile(
        BTC_SQUEEZE_DON14, "BTC-USDT-SWAP", "BTC 专属 D3 · 4H挤压突破 + 1D唐奇安14",
        "BTC 专属 D3：4H TTM Squeeze 波动率压缩释放 + 1D Donchian(14) CTA，单笔风险 9% × 15 倍逐仓（名义上限 10 倍，止损上限 5.0%）",
        Decimal("15"), Decimal("9"), Decimal("10"), 0.050,
    ),
    BTC_DAILY_DON20: DedicatedProfile(
        BTC_DAILY_DON20, "BTC-USDT-SWAP", "BTC 专属 D6 · 1D唐奇安20 慢速海龟",
        "BTC 专属 D6：1D Donchian(20) + 1D EMA50 慢速海龟 CTA，单笔风险 9% × 12 倍逐仓（名义上限 8 倍，止损上限 6.5%）",
        Decimal("12"), Decimal("9"), Decimal("8"), 0.065,
    ),
    ETH_WIDE_ST_DON14: DedicatedProfile(
        ETH_WIDE_ST_DON14, "ETH-USDT-SWAP", "ETH 专属 D3/D6 · 4H宽超趋势 + 1D唐奇安14",
        "ETH 专属 D3/D6：4H Supertrend(14,3.8)+1D EMA50 与 1D Donchian(14) CTA，单笔风险 9% × 15 倍逐仓（名义上限 10 倍，止损上限 5.0%）",
        Decimal("15"), Decimal("9"), Decimal("10"), 0.050,
    ),
    SOL_DAILY_ST30: DedicatedProfile(
        SOL_DAILY_ST30, "SOL-USDT-SWAP", "SOL 专属 D3/D6 · 1D宽超趋势 ST(10,3.0)",
        "SOL 专属 D3/D6：1D Supertrend(10,3.0) 宏观趋势跟踪，单笔风险 9% × 10 倍逐仓（名义上限 7 倍，止损上限 8.5%）",
        Decimal("10"), Decimal("9"), Decimal("7"), 0.085,
    ),
}
DEDICATED_STRATEGIES = tuple(DEDICATED_PROFILES)


def _policy_id(profile):
    return hashlib.sha256(json.dumps({
        "version": DEDICATED_VERSION, "strategy": profile.strategy, "instrument": profile.instrument,
        "leverage": str(profile.leverage), "risk_percent": str(profile.risk_percent),
        "max_notional_multiple": str(profile.max_notional_multiple),
        "stop_range": [profile.min_stop_pct, profile.max_stop_pct],
        "sq4h": asdict(SQ4H_BRACKETS), "don20": asdict(DON20_BRACKETS),
        "st14": asdict(ST14_BRACKETS), "st1d30": asdict(ST1D30_BRACKETS), "don14": asdict(M5_BRACKETS),
        "entry_window": FIXED_ENTRY_WINDOW,
    }, sort_keys=True).encode()).hexdigest()[:12]


DEDICATED_POLICIES = {
    key: FixedSignalPolicy(key, profile.instrument, DEDICATED_VERSION, _policy_id(profile), M5_BRACKETS)
    for key, profile in DEDICATED_PROFILES.items()
}


def _frame(rows, timeframe):
    bars = np.asarray(rows, dtype=float).copy()
    bars[:, 0] += TIMEFRAMES[timeframe]
    return indicators(bars)


def _eval_daily_donchian(d1, period, h4_closed):
    d1_closed = int(d1["times"][-1])
    d_idx = int(np.searchsorted(d1["times"], h4_closed, side="right") - 1)
    d_valid = d_idx >= 0 and np.isfinite(d1["slow"][d_idx]) and np.isfinite(d1["close"][d_idx])
    d_dir = (1 if float(d1["close"][d_idx]) > float(d1["slow"][d_idx]) else -1) if d_valid else 0

    hi_n = rolling(shift(d1["high"]), period, "max")
    lo_n = rolling(shift(d1["low"]), period, "min")
    c_cur, c_prev = float(d1["close"][-1]), float(d1["close"][-2])
    hi_cur, hi_prev = float(hi_n[-1]), float(hi_n[-2])
    lo_cur, lo_prev = float(lo_n[-1]), float(lo_n[-2])
    d1_atr = float(d1["atr"][-1])
    atr_ok = np.isfinite(d1_atr) and d1_atr > 0
    on_daily_close = h4_closed == d1_closed

    if d_dir == 1 and np.isfinite(hi_cur) and np.isfinite(hi_prev):
        cross = c_prev <= hi_prev and c_cur > hi_cur
    elif d_dir == -1 and np.isfinite(lo_cur) and np.isfinite(lo_prev):
        cross = c_prev >= lo_prev and c_cur < lo_cur
    else:
        cross = False
    valid = d_valid and on_daily_close and cross and atr_ok
    return {
        "d_valid": d_valid, "d_dir": d_dir, "d1_closed": d1_closed, "d1_atr": d1_atr,
        "c_cur": c_cur, "hi_cur": hi_cur, "lo_cur": lo_cur, "cross": cross, "valid": valid,
    }


def dedicated_setup(windows, strategy):
    """Evaluate one of the 4 dedicated per-coin strategies on closed 4H/1D windows."""
    h4 = _frame(windows["4h"], "4h")
    d1 = _frame(windows["1d"], "1d")
    h4_closed = int(h4["times"][-1])
    h4_atr = float(h4["atr"][-1])
    h4_atr_ok = np.isfinite(h4_atr) and h4_atr > 0
    h4_close = float(h4["close"][-1])

    if strategy == BTC_SQUEEZE_DON14:
        bb_up, _, bb_lo = talib.BBANDS(h4["close"], 20, 2.0, 2.0)
        kc_up = h4["ema"] + 1.5 * h4["atr"]
        kc_dn = h4["ema"] - 1.5 * h4["atr"]
        sq = (bb_up < kc_up) & (bb_lo > kc_dn)
        sq_prev3 = bool(np.any(sq[-4:-1]))
        sq_released = sq_prev3 and not bool(sq[-1])
        ema50 = float(h4["slow"][-1])
        sq_long = sq_released and np.isfinite(bb_up[-1]) and np.isfinite(ema50) and h4_close > float(bb_up[-1]) and h4_close > ema50
        sq_short = sq_released and np.isfinite(bb_lo[-1]) and np.isfinite(ema50) and h4_close < float(bb_lo[-1]) and h4_close < ema50
        sq_valid = (sq_long or sq_short) and h4_atr_ok
        sq_dir = 1 if sq_long else -1 if sq_short else (1 if np.isfinite(ema50) and h4_close > ema50 else -1 if np.isfinite(ema50) else 0)

        don = _eval_daily_donchian(d1, 14, h4_closed)
        chosen = "m7_4h_squeeze" if sq_valid else "m5_1d_donchian" if don["valid"] else None
        use_don = chosen == "m5_1d_donchian"
        side_dir = don["d_dir"] if use_don else (sq_dir if sq_valid else don["d_dir"] or sq_dir)
        side = "long" if side_dir == 1 else "short" if side_dir == -1 else "mixed"
        brackets = M5_BRACKETS if use_don else SQ4H_BRACKETS
        return {
            "strategy": strategy if chosen else None, "engine": chosen, "side": side,
            "timeframe": "1d" if use_don else "4h",
            "closed_at": don["d1_closed"] if use_don else h4_closed,
            "close": don["c_cur"] if use_don else h4_close,
            "atr": don["d1_atr"] if use_don else h4_atr,
            "breakeven_r": M5_BREAKEVEN_R if use_don else SQ4H_BREAKEVEN_R,
            "trailing_atr": brackets.trailing_atr, "target_r": brackets.target_r,
            "stop_atr": brackets.stop_atr, "hold_minutes": brackets.hold,
            "cooldown_minutes": M5_COOLDOWN_MINUTES if use_don else SQ4H_COOLDOWN_MINUTES,
            "engines": [
                {"engine": "m7_4h_squeeze", "label": "4H TTM压缩突破", "timeframe": "4h", "side": side,
                 "closed_at": h4_closed, "squeeze_now": bool(sq[-1]), "squeeze_released": sq_released, "active": bool(sq_valid)},
                {"engine": "m5_1d_donchian", "label": "1D 唐奇安14 CTA", "timeframe": "1d", "side": side,
                 "closed_at": don["d1_closed"], "trend_pass": bool(don["d_valid"] and don["d_dir"] != 0),
                 "breakout_pass": bool(don["cross"]),
                 "don_low": round(don["lo_cur"], 4) if np.isfinite(don["lo_cur"]) else None,
                 "don_high": round(don["hi_cur"], 4) if np.isfinite(don["hi_cur"]) else None,
                 "active": bool(don["valid"])},
            ],
        }

    if strategy == BTC_DAILY_DON20:
        don = _eval_daily_donchian(d1, 20, h4_closed)
        side = "long" if don["d_dir"] == 1 else "short" if don["d_dir"] == -1 else "mixed"
        chosen = "m2_1d_donchian20" if don["valid"] else None
        return {
            "strategy": strategy if chosen else None, "engine": chosen, "side": side,
            "timeframe": "1d", "closed_at": don["d1_closed"], "close": don["c_cur"], "atr": don["d1_atr"],
            "breakeven_r": DON20_BREAKEVEN_R, "trailing_atr": DON20_BRACKETS.trailing_atr,
            "target_r": DON20_BRACKETS.target_r, "stop_atr": DON20_BRACKETS.stop_atr,
            "hold_minutes": DON20_BRACKETS.hold, "cooldown_minutes": DON20_COOLDOWN_MINUTES,
            "engines": [
                {"engine": "m2_1d_donchian20", "label": "1D 唐奇安20 慢速海龟", "timeframe": "1d", "side": side,
                 "closed_at": don["d1_closed"], "trend_pass": bool(don["d_valid"] and don["d_dir"] != 0),
                 "breakout_pass": bool(don["cross"]),
                 "don_low": round(don["lo_cur"], 4) if np.isfinite(don["lo_cur"]) else None,
                 "don_high": round(don["hi_cur"], 4) if np.isfinite(don["hi_cur"]) else None,
                 "active": bool(don["valid"])},
            ],
        }

    if strategy == ETH_WIDE_ST_DON14:
        don = _eval_daily_donchian(d1, 14, h4_closed)
        d_dir = don["d_dir"]
        st_dir = supertrend_direction(h4["high"], h4["low"], h4["close"], period=14, mult=3.8)
        st_cur, st_prev = int(st_dir[-1]), int(st_dir[-2])
        st_flip = (d_dir == 1 and st_cur == 1 and st_prev != 1) or (d_dir == -1 and st_cur == -1 and st_prev != -1)
        st_valid = don["d_valid"] and st_flip and h4_atr_ok
        chosen = "m7_4h_st14_38" if st_valid else "m5_1d_donchian" if don["valid"] else None
        use_don = chosen == "m5_1d_donchian"
        side = "long" if d_dir == 1 else "short" if d_dir == -1 else "mixed"
        brackets = M5_BRACKETS if use_don else ST14_BRACKETS
        return {
            "strategy": strategy if chosen else None, "engine": chosen, "side": side,
            "timeframe": "1d" if use_don else "4h",
            "closed_at": don["d1_closed"] if use_don else h4_closed,
            "close": don["c_cur"] if use_don else h4_close,
            "atr": don["d1_atr"] if use_don else h4_atr,
            "breakeven_r": M5_BREAKEVEN_R if use_don else ST14_BREAKEVEN_R,
            "trailing_atr": brackets.trailing_atr, "target_r": brackets.target_r,
            "stop_atr": brackets.stop_atr, "hold_minutes": brackets.hold,
            "cooldown_minutes": M5_COOLDOWN_MINUTES if use_don else ST14_COOLDOWN_MINUTES,
            "engines": [
                {"engine": "m7_4h_st14_38", "label": "4H 宽超趋势 ST(14,3.8)", "timeframe": "4h", "side": side,
                 "closed_at": h4_closed, "trend_pass": bool(don["d_valid"] and d_dir != 0),
                 "flip_pass": bool(st_flip), "st_dir": st_cur, "active": bool(st_valid)},
                {"engine": "m5_1d_donchian", "label": "1D 唐奇安14 CTA", "timeframe": "1d", "side": side,
                 "closed_at": don["d1_closed"], "trend_pass": bool(don["d_valid"] and d_dir != 0),
                 "breakout_pass": bool(don["cross"]),
                 "don_low": round(don["lo_cur"], 4) if np.isfinite(don["lo_cur"]) else None,
                 "don_high": round(don["hi_cur"], 4) if np.isfinite(don["hi_cur"]) else None,
                 "active": bool(don["valid"])},
            ],
        }

    if strategy == SOL_DAILY_ST30:
        d1_closed = int(d1["times"][-1])
        d1_atr = float(d1["atr"][-1])
        atr_ok = np.isfinite(d1_atr) and d1_atr > 0
        st_dir = supertrend_direction(d1["high"], d1["low"], d1["close"], period=10, mult=3.0)
        st_cur, st_prev = int(st_dir[-1]), int(st_dir[-2])
        on_daily_close = h4_closed == d1_closed
        flip = st_cur in (1, -1) and st_cur != st_prev
        valid = on_daily_close and flip and atr_ok
        side = "long" if st_cur == 1 else "short" if st_cur == -1 else "mixed"
        chosen = "m4_1d_st30" if valid else None
        return {
            "strategy": strategy if chosen else None, "engine": chosen, "side": side,
            "timeframe": "1d", "closed_at": d1_closed, "close": float(d1["close"][-1]), "atr": d1_atr,
            "breakeven_r": ST1D30_BREAKEVEN_R, "trailing_atr": ST1D30_BRACKETS.trailing_atr,
            "target_r": ST1D30_BRACKETS.target_r, "stop_atr": ST1D30_BRACKETS.stop_atr,
            "hold_minutes": ST1D30_BRACKETS.hold, "cooldown_minutes": ST1D30_COOLDOWN_MINUTES,
            "engines": [
                {"engine": "m4_1d_st30", "label": "1D 宽超趋势 ST(10,3.0)", "timeframe": "1d", "side": side,
                 "closed_at": d1_closed, "trend_pass": st_cur != 0,
                 "flip_pass": bool(flip), "st_dir": st_cur, "active": bool(valid)},
            ],
        }

    raise ValueError(f"未知专属策略：{strategy}")


def dedicated_signal_candidate(instrument, strategy, setup, market_quote, rules, now, *, binance=False):
    profile = DEDICATED_PROFILES[strategy]
    policy = DEDICATED_POLICIES[strategy]
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
        "funding_cost_rate": 0.0, "policy_id": policy.policy_id, "policy_version": DEDICATED_VERSION,
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
