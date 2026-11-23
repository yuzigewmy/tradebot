"""Price and funding bounds shared by research and live signal calculation."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR

from .perpetual_history import MINUTE, SECOND
from .perpetual_market import FUNDING_INTERVAL
from .perpetual_paper import FEE_RATE, SLIPPAGE, PaperAccount, d
from .perpetual_regime import Brackets

CAPITAL, MARGIN, LEVERAGE = (d(n) for n in (1000, 250, 30))
FUNDING_CHARGE = d("0.002")
MIN_STOP_PCT, MAX_STOP_PCT = 0.004, 0.024
FIXED_COOLDOWN_MINUTES = 30
FIXED_FUNDING_BUFFER = 2
FIXED_ENTRY_WINDOW = 10 * SECOND


@dataclass(frozen=True)
class FixedSignalPolicy:
    strategy: str
    instrument: str
    version: str
    policy_id: str
    brackets: Brackets


def adverse_tick(price, sign, tick, slip):
    price = d(price) * (1 + sign * slip)
    return (price / tick).to_integral_value(rounding=ROUND_CEILING if sign == 1 else ROUND_FLOOR) * tick


def price_brackets(entry, sign, atr, brackets):
    distance = min(MAX_STOP_PCT * float(entry), max(MIN_STOP_PCT * float(entry), brackets.stop_atr * atr))
    return entry - sign * d(distance), entry + sign * d(distance) * d(brackets.target_r)


def holding_deadline(entry_at, hold_minutes, funding_buffer):
    deadline = entry_at + hold_minutes * MINUTE
    if funding_buffer:
        settlement = (entry_at // FUNDING_INTERVAL + 1) * FUNDING_INTERVAL
        deadline = min(deadline, settlement - funding_buffer * MINUTE)
    return deadline

ENTRY_ISSUES = {
    "insufficient_depth": "盘口深度不足，无法覆盖计划仓位",
    "above_tier_size": "计划仓位超出合约档位",
    "insufficient_cash": "模拟余额不足以覆盖计划逐仓额度",
    "below_minimum_size": "计划仓位小于交易所最小合约数量",
    "leverage_exceeds_tier": "杠杆超出合约档位",
    "stop_beyond_liquidation": "止损价越过模拟强平价",
}


def fixed_signal_candidate(policy, setup, market_quote, rules, now):
    closed_at = setup["closed_at"]
    entry_at = closed_at + MINUTE
    expires_at = entry_at + FIXED_ENTRY_WINDOW
    # The outcome journal opens simulated positions on whole seconds.
    if not entry_at <= market_quote["ticker_at"] <= now or (now + SECOND - 1) // SECOND * SECOND >= expires_at:
        return None, "未取得入场时间窗内的新盘口"
    exit_at = holding_deadline(entry_at, policy.brackets.hold, FIXED_FUNDING_BUFFER)
    exit_at = min(exit_at, market_quote["funding_at"] - FIXED_FUNDING_BUFFER * MINUTE)
    if now >= exit_at:
        return None, "距资金费结算不足两分钟"
    side = setup["side"]
    sign = 1 if side == "long" else -1
    entry_side = "ask" if sign == 1 else "bid"
    entry = adverse_tick(market_quote[entry_side], sign, rules.tick, SLIPPAGE)
    stop, target = price_brackets(entry, sign, setup["atr"], policy.brackets)
    stop = adverse_tick(stop, -sign, rules.tick, 0)
    target = adverse_tick(target, sign, rules.tick, 0)
    stop_fill = adverse_tick(stop, -sign, rules.tick, SLIPPAGE)
    target_fill = adverse_tick(target, -sign, rules.tick, SLIPPAGE)
    risk = sign * (entry - stop_fill) + (entry + stop_fill) * FEE_RATE
    reward = sign * (target_fill - entry) - (entry + target_fill) * FEE_RATE
    candidate = {
        "side": side, "strategy": policy.strategy, "closed_at": closed_at,
        "entry_at": entry_at, "expires_at": expires_at, "exit_at": exit_at,
        "entry": float(entry), "stop_loss": float(stop), "take_profit": float(target),
        "risk_per_unit": float(risk), "net_reward_risk": round(float(reward / risk), 4), "funding_cost_rate": 0.0,
        "policy_id": policy.policy_id, "policy_version": policy.version,
        "entry_reference": f"best_{entry_side}", "exit_reference": "last_trade",
    }
    account = PaperAccount({policy.instrument: rules}, cash=CAPITAL, leverage=LEVERAGE,
                           fixed_margin=MARGIN, funding_charge=FUNDING_CHARGE)
    state = account.enter(policy.instrument, candidate, market_quote | {entry_side: entry}, now)
    if state != "entered":
        return None, ENTRY_ISSUES[state]
    position = account.positions[policy.instrument]
    return candidate | {"margin_budget": int(MARGIN), "leverage": int(LEVERAGE), "contracts": float(position.contracts)}, None
