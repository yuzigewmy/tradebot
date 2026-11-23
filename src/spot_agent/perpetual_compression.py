"""Frozen ETH volatility compression rules from the November research replay."""

from dataclasses import asdict
import hashlib
import json

import numpy as np

from .perpetual_history import MINUTE, SECOND
from .perpetual_paper import FEE_RATE, SLIPPAGE, PaperAccount
from .perpetual_regime import BREAKOUT_ATR_BUFFER, Brackets, Setup, compression_range, indicators, prior_channel
from .perpetual_regime_terms import (CAPITAL, FUNDING_CHARGE, LEVERAGE, MARGIN, MAX_STOP_PCT, MIN_STOP_PCT,
                                     adverse_tick, holding_deadline, price_brackets)

COMPRESSION_STRATEGY = "eth_compression_short"
COMPRESSION_INSTRUMENT = "ETH-USDT-SWAP"
COMPRESSION_SETUP = Setup("compression", 30, 24, "any", 1.25)
COMPRESSION_BRACKETS = Brackets(1.5, 2.5, 240)
COMPRESSION_COOLDOWN = 30 * MINUTE
COMPRESSION_FUNDING_BUFFER = 2
COMPRESSION_ENTRY_WINDOW = 10 * SECOND
COMPRESSION_VERSION = "perpetual-compression/v1"
COMPRESSION_POLICY_ID = hashlib.sha256(json.dumps({
    "version": COMPRESSION_VERSION, "setup": asdict(COMPRESSION_SETUP), "brackets": asdict(COMPRESSION_BRACKETS),
    "funding_buffer": COMPRESSION_FUNDING_BUFFER, "cooldown": COMPRESSION_COOLDOWN, "margin": str(MARGIN),
    "leverage": str(LEVERAGE), "fee": str(FEE_RATE), "slippage": str(SLIPPAGE),
    "stop_range": [MIN_STOP_PCT, MAX_STOP_PCT], "breakout_buffer": BREAKOUT_ATR_BUFFER,
    "entry_window": COMPRESSION_ENTRY_WINDOW,
}, sort_keys=True).encode()).hexdigest()[:12]


ENTRY_ISSUES = {
    "insufficient_depth": "盘口深度不足，无法覆盖计划仓位",
    "above_tier_size": "计划仓位超出合约档位",
    "insufficient_cash": "模拟余额不足以覆盖计划逐仓额度",
    "below_minimum_size": "计划仓位小于交易所最小合约数量",
    "leverage_exceeds_tier": "杠杆超出合约档位",
    "stop_beyond_liquidation": "止损价越过模拟强平价",
}


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


def compression_candidate(setup, market_quote, rules, now):
    closed_at = setup["closed_at"]
    entry_at = closed_at + MINUTE
    expires_at = entry_at + COMPRESSION_ENTRY_WINDOW
    if not entry_at <= market_quote["ticker_at"] <= now < expires_at:
        return None, "未取得入场时间窗内的新盘口"
    exit_at = holding_deadline(entry_at, COMPRESSION_BRACKETS.hold, COMPRESSION_FUNDING_BUFFER)
    exit_at = min(exit_at, market_quote["funding_at"] - COMPRESSION_FUNDING_BUFFER * MINUTE)
    if now >= exit_at:
        return None, "距资金费结算不足两分钟"
    entry = adverse_tick(market_quote["bid"], -1, rules.tick, SLIPPAGE)
    stop, target = price_brackets(entry, -1, setup["atr"], COMPRESSION_BRACKETS)
    stop = adverse_tick(stop, 1, rules.tick, 0)
    target = adverse_tick(target, -1, rules.tick, 0)
    adjusted_stop = adverse_tick(stop, 1, rules.tick, SLIPPAGE)
    adjusted_target = adverse_tick(target, 1, rules.tick, SLIPPAGE)
    risk = adjusted_stop - entry + (entry + adjusted_stop) * FEE_RATE
    reward = entry - adjusted_target - (entry + adjusted_target) * FEE_RATE
    candidate = {
        "side": "short", "strategy": COMPRESSION_STRATEGY, "closed_at": closed_at,
        "entry_at": entry_at, "expires_at": expires_at, "exit_at": exit_at,
        "entry": float(entry), "stop_loss": float(stop), "take_profit": float(target),
        "risk_per_unit": float(risk), "net_reward_risk": round(float(reward / risk), 4), "funding_cost_rate": 0.0,
        "policy_id": COMPRESSION_POLICY_ID, "policy_version": COMPRESSION_VERSION,
        "entry_reference": "best_bid", "exit_reference": "last_trade",
    }
    account = PaperAccount({COMPRESSION_INSTRUMENT: rules}, cash=CAPITAL, leverage=LEVERAGE,
                           fixed_margin=MARGIN, funding_charge=FUNDING_CHARGE)
    state = account.enter(COMPRESSION_INSTRUMENT, candidate, market_quote | {"bid": entry}, now)
    if state != "entered":
        return None, ENTRY_ISSUES[state]
    position = account.positions[COMPRESSION_INSTRUMENT]
    return candidate | {"margin_budget": int(MARGIN), "leverage": int(LEVERAGE), "contracts": float(position.contracts)}, None
