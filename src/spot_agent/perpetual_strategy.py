"""Closed-bar setup detection and cost-adjusted reference levels."""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path

import numpy as np
import talib

from .contracts import TIMEFRAMES

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "perpetual-entry"
SKILL = (SKILL_PATH / "SKILL.md").read_text(encoding="utf-8")
POLICY = json.loads((SKILL_PATH / "policy.json").read_text(encoding="utf-8"))
POLICY_ID = hashlib.sha256((SKILL + json.dumps(POLICY, sort_keys=True)).encode()).hexdigest()[:12]
TREND_SKILL = (SKILL_PATH / "15m-review.md").read_text(encoding="utf-8")
TREND_POLICY = POLICY | json.loads((SKILL_PATH / "15m-policy.json").read_text(encoding="utf-8"))
TREND_POLICY_ID = hashlib.sha256((TREND_SKILL + json.dumps(TREND_POLICY, sort_keys=True)).encode()).hexdigest()[:12]
PROFILES = {"v1": (POLICY, POLICY_ID, SKILL), "v2": (TREND_POLICY, TREND_POLICY_ID, TREND_SKILL)}


def _indicators(rows, policy=POLICY):
    high, low, close, volume = np.asarray(rows, dtype=float)[:, 2:6].T
    fast = talib.EMA(close, timeperiod=policy["ema_fast"])
    slow = talib.EMA(close, timeperiod=policy["ema_slow"])
    atr = talib.ATR(high, low, close, timeperiod=14)
    upper, center, lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
    baseline = volume[-21:-1].mean()
    values = {
        "close": float(close[-1]), "ema_fast": float(fast[-1]), "ema_slow": float(slow[-1]), "prev_ema_fast": float(fast[-2]),
        "adx": float(talib.ADX(high, low, close, timeperiod=14)[-1]), "rsi": float(talib.RSI(close, timeperiod=14)[-1]),
        "atr": float(atr[-1]), "atr_reference": float(np.median(atr[-61:-1])),
        "volume_ratio": float(volume[-1] / baseline) if baseline > 0 else 0.0,
        "widths": (upper - lower) / center,
    }
    if "trend_slope_bars" in policy:
        values["ema_slope"] = float(fast[-1] - fast[-1 - policy["trend_slope_bars"]])
    return values


def _bias(values):
    if values["close"] > values["ema_fast"] > values["ema_slow"]:
        return "long"
    if values["close"] < values["ema_fast"] < values["ema_slow"]:
        return "short"
    return "mixed"


def detect_setup(windows):
    indicators = {tf: _indicators(rows) for tf, rows in windows.items()}
    context = {tf: {
        "closed_at": windows[tf][-1][0] + TIMEFRAMES[tf], "bias": _bias(values),
        **{key: values[key] for key in ("close", "ema_fast", "ema_slow", "adx", "rsi", "atr", "volume_ratio")},
    } for tf, values in indicators.items()}
    side = context["1h"]["bias"]
    if side != context["15m"]["bias"]:
        side = "mixed"
    micro = indicators["1m"]
    bars = windows["1m"]
    prev_band = float(micro["widths"][-2])
    width_threshold = float(np.quantile(micro["widths"][-POLICY["squeeze_window"]-2:-2], POLICY["squeeze_quantile"]))
    squeezed = prev_band <= width_threshold and width_threshold > 0
    volatile = micro["atr"] > 1.5 * micro["atr_reference"]
    lookback = bars[-POLICY["breakout_window"]-1:-1]
    prior_high, prior_low = max(row[2] for row in lookback), min(row[3] for row in lookback)
    close, atr, ratio = (micro[key] for key in ("close", "atr", "volume_ratio"))
    expansion = POLICY["breakout_atr_buffer"] * atr
    prev = bars[-2]
    fast, prev_fast = micro["ema_fast"], micro["prev_ema_fast"]
    trigger, reclaimed = False, False
    if side == "long":
        trigger, reclaimed = close > prior_high + expansion, prev[4] <= prev_fast and close > fast
    elif side == "short":
        trigger, reclaimed = close < prior_low - expansion, prev[4] >= prev_fast and close < fast
    pullback_adx_pass = context["15m"]["adx"] >= POLICY["pullback_adx_min"]
    pullback_volume_pass = ratio >= POLICY["pullback_volume_min"]
    breakout_volume_pass = ratio >= POLICY["breakout_volume_min"]
    setup = {
        "side": side, "strategy": None, "close": close, "atr": atr, "volume_ratio": ratio,
        "squeezed": squeezed, "volatile": volatile, "prior_band_width": prev_band, "squeeze_threshold": width_threshold,
        "prior_high": prior_high, "prior_low": prior_low, "range_cross": trigger, "ema_reclaimed": reclaimed,
        "pullback_adx_15m_pass": pullback_adx_pass, "pullback_volume_1m_pass": pullback_volume_pass,
        "breakout_volume_1m_pass": breakout_volume_pass,
    }
    if side == "mixed" or atr <= 0:
        return {"context": context, "setup": setup}
    recent = bars[-POLICY["pullback_stop_window"]:]
    if squeezed and trigger and breakout_volume_pass:
        setup.update(strategy="breakout", stop_anchor=prior_high if side == "long" else prior_low)
    elif reclaimed and pullback_adx_pass and pullback_volume_pass:
        anchor = min(row[3] for row in recent) if side == "long" else max(row[2] for row in recent)
        setup.update(strategy="pullback", stop_anchor=anchor)
    return {"context": context, "setup": setup}


def detect_trend_setup(windows):
    policy = TREND_POLICY
    context = {}
    for tf, rows in windows.items():
        values = _indicators(rows, policy)
        context[tf] = {"closed_at": rows[-1][0] + TIMEFRAMES[tf], "bias": _bias(values),
                       **{k: values[k] for k in ("close", "ema_fast", "ema_slow", "ema_slope", "adx", "rsi", "atr", "volume_ratio")}}
    hourly, trigger = context["1h"], context["15m"]
    sign = {"long": 1, "short": -1, "mixed": 0}[hourly["bias"]]
    aligned = bool(sign and hourly["bias"] == trigger["bias"] and sign * hourly["ema_slope"] > 0)
    trend = aligned and hourly["adx"] >= policy["trend_adx_min"]
    fresh_close = context["1m"]["closed_at"] == trigger["closed_at"]
    bars = windows["15m"]
    prior = bars[-policy["breakout_window"] - 1:-1]
    high = max(row[2] for row in prior)
    low = min(row[3] for row in prior)
    close, atr = trigger["close"], trigger["atr"]
    edge = high if sign > 0 else low
    crossed = bool(sign and sign * (close - edge) > atr * policy["breakout_atr_buffer"])
    wick, previous_edge = (bars[-1][3], bars[-2][2]) if sign > 0 else (bars[-1][2], bars[-2][3])
    reclaimed = bool(sign and sign * (wick - trigger["ema_fast"]) <= 0 and sign * (close - previous_edge) > 0)
    volume_ratio = trigger["volume_ratio"]
    setup = {"side": hourly["bias"] if aligned else "mixed", "strategy": None, "close": close, "atr": atr,
             "volume_ratio": volume_ratio, "prior_high": high, "prior_low": low, "range_cross": crossed, "ema_reclaimed": reclaimed,
             "regime": "trend" if trend else "observe", "trend_pass": trend, "fresh_trigger": fresh_close,
             "breakout_volume_pass": volume_ratio >= policy["breakout_volume_min"],
             "pullback_volume_pass": volume_ratio >= policy["pullback_volume_min"]}
    if trend and fresh_close and atr > 0:
        if crossed and setup["breakout_volume_pass"]:
            setup.update(strategy="channel_breakout", stop_anchor=edge)
        elif reclaimed and setup["pullback_volume_pass"]:
            recent = bars[-policy["pullback_stop_window"]:]
            anchor = min(row[3] for row in recent) if sign > 0 else max(row[2] for row in recent)
            setup.update(strategy="trend_pullback", stop_anchor=anchor)
    return {"context": context, "setup": setup}


def _round_to_tick(price, tick, upward):
    units = Decimal(str(price)) / tick
    return float(units.to_integral_value(rounding=ROUND_CEILING if upward else ROUND_FLOOR) * tick)


def quote_issue(quote, now):
    bid, ask, mark = (quote[key] for key in ("bid", "ask", "mark"))
    if now - min(quote["ticker_at"], quote["mark_at"]) > 15_000:
        return "盘口或标记价已过期"
    if not 0 < bid <= ask or (ask - bid) / bid > POLICY["max_spread_rate"]:
        return "盘口价差超限"
    if abs(mark / ((bid + ask) / 2) - 1) > POLICY["max_mark_basis_rate"]:
        return "标记价与盘口中间价偏离过大"
    return None


def funding_cost_rate(long, quote, now, policy=POLICY):
    if now <= quote["funding_at"] <= now + policy["funding_horizon_minutes"] * 60_000:
        return max(0.0, (1 if long else -1) * quote["funding_rate"])
    return 0.0


def priced_candidate(setup, quote, tick, closed_at, now, *, profile="v1"):
    policy, policy_id, _ = PROFILES[profile]
    if now >= closed_at + TIMEFRAMES["1m"]:
        return None, "开仓窗口已过期"
    if issue := quote_issue(quote, now):
        return None, issue
    long = setup["side"] == "long"
    sign = 1 if long else -1
    entry = _round_to_tick(quote["ask"] if long else quote["bid"], tick, long)
    atr = setup["atr"]
    if abs(entry - setup["close"]) > policy["max_entry_drift_atr"] * atr:
        return None, "盘口已离开触发收盘价附近"
    structural_stop = setup["stop_anchor"] - sign * policy["stop_atr_buffer"] * atr
    stop_distance = max(sign * (entry - structural_stop), atr * policy["min_stop_atr"])
    stop = _round_to_tick(entry - sign * stop_distance, tick, not long)
    distance = sign * (entry - stop)
    if stop <= 0 or not 0 < distance <= atr * policy["max_stop_atr"]:
        return None, "结构止损距离超出波动限制"
    funding_cost = funding_cost_rate(long, quote, now, policy)
    fee, slippage = policy["fee_rate"], policy["slippage_rate"]
    entry_slippage = slippage if profile == "v1" else 0
    entry_multiplier = (1 + sign * entry_slippage) * (1 + sign * fee)
    exit_multiplier = (1 - sign * slippage) * (1 - sign * fee)
    funding = entry * funding_cost
    risk = sign * (entry * entry_multiplier - stop * exit_multiplier) + funding
    extras = {}
    if profile == "v2":
        cost_ratio = (risk - distance + quote["ask"] - quote["bid"]) / distance
        if cost_ratio > policy["max_cost_to_stop"]:
            return None, "预估交易成本超过结构止损距离的四分之一"
        extras = {"cost_to_stop": cost_ratio, "exit_at": closed_at + policy["max_hold_minutes"] * TIMEFRAMES["1m"]}
    target = (entry * entry_multiplier + sign * (risk * policy["take_profit_r"] + funding)) / exit_multiplier
    target = _round_to_tick(target, tick, long)
    if target <= 0:
        return None, "按费用计算的目标价不可用"
    reward = sign * (target * exit_multiplier - entry * entry_multiplier) - funding
    return {
        "side": setup["side"], "strategy": setup["strategy"], "entry": entry, "stop_loss": stop, "take_profit": target,
        "net_reward_risk": round(reward / risk, 4), "risk_per_unit": risk, "funding_cost_rate": funding_cost,
        "entry_reference": "best_ask" if long else "best_bid", "exit_reference": "last_trade",
        "closed_at": closed_at, "expires_at": closed_at + TIMEFRAMES["1m"],
        "policy_version": policy["version"], "policy_id": policy_id, **extras,
    }, None


def candidate_issue(candidate, setup, quote, now, *, profile="v1"):
    policy, _, _ = PROFILES[profile]
    if now >= candidate["expires_at"]:
        return "模型复核结束时，开仓窗口已过期"
    if issue := quote_issue(quote, now):
        return issue
    long = candidate["side"] == "long"
    bid_or_ask = quote["ask"] if long else quote["bid"]
    if (bid_or_ask > candidate["entry"] if long else bid_or_ask < candidate["entry"]):
        return "复核后盘口无法按候选价或更优价入场"
    if abs(bid_or_ask - setup["close"]) > policy["max_entry_drift_atr"] * setup["atr"]:
        return "复核后盘口已离开触发收盘价附近"
    bottom, top = sorted((candidate["stop_loss"], candidate["take_profit"]))
    if not (bottom < quote["last"] < top and bottom < bid_or_ask < top):
        return "复核后成交价或盘口已到达退出区间"
    if funding_cost_rate(long, quote, now, policy) > candidate["funding_cost_rate"]:
        return "复核后预计承担的资金费上升"
    return None


def exits_touched(candidate, candle):
    if candle is None or candle[0] != candidate["closed_at"]:
        return False
    lower, upper = sorted((candidate["stop_loss"], candidate["take_profit"]))
    return candle[3] <= lower or candle[2] >= upper


def signal_cooldown(previous, closed_at, *, profile="v1"):
    policy, _, _ = PROFILES[profile]
    return previous is not None and closed_at - previous < policy["signal_cooldown_minutes"] * TIMEFRAMES["1m"]
