"""Shared isolated-margin paper account; no exchange order interface."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .perpetual_strategy import POLICY

INITIAL_CASH = Decimal("100")
LEVERAGE = Decimal("25")
RISK_FRACTION = Decimal("0.01")
FEE_RATE = Decimal(str(POLICY["fee_rate"]))
SLIPPAGE = Decimal(str(POLICY["slippage_rate"]))
ZERO = Decimal(0)


def d(value):
    return Decimal(str(value))


@dataclass
class Position:
    instrument: str
    side: str
    strategy: str
    closed_at: int
    opened_at: int
    contracts: Decimal
    base: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    margin: Decimal
    entry_fee: Decimal
    maintenance_rate: Decimal
    funding: Decimal = ZERO

    @property
    def sign(self):
        return 1 if self.side == "long" else -1

    def pnl(self, price):
        return self.sign * (d(price) - self.entry) * self.base

    def liquidation(self):
        return (self.sign * self.entry - (self.margin + self.funding) / self.base) / (self.sign - self.maintenance_rate - FEE_RATE)


class PaperAccount:
    def __init__(self, rules, cash=INITIAL_CASH, leverage=LEVERAGE, risk=RISK_FRACTION):
        self.rules, self.cash, self.leverage, self.risk = rules, cash, leverage, risk
        self.positions, self.marks = {}, {}
        self.trades, self.events = [], []

    def equity(self):
        return self.cash + sum(max(ZERO, p.margin + p.funding + p.pnl(self.marks[symbol])) for symbol, p in self.positions.items())

    def enter(self, instrument, candidate, quote, ts):
        if instrument in self.positions:
            return "instrument_position_open"
        rules = self.rules[instrument]
        price_key, size_key = ("ask", "ask_size") if candidate["side"] == "long" else ("bid", "bid_size")
        entry = d(quote[price_key])
        notional_per_contract = entry * rules.contract_value
        margin_per_contract = notional_per_contract / self.leverage
        fee_per_contract = notional_per_contract * FEE_RATE
        cap = min(self.equity() * self.risk / (d(candidate["risk_per_unit"]) * rules.contract_value),
                  self.cash / (margin_per_contract + fee_per_contract), d(quote[size_key]), rules.max_tier_size)
        contracts = (cap / rules.lot).to_integral_value(rounding=ROUND_FLOOR) * rules.lot
        if contracts < rules.min_size:
            return "below_minimum_size"
        if self.leverage > rules.max_leverage:
            return "leverage_exceeds_tier"
        position = Position(instrument, candidate["side"], candidate["strategy"], candidate["closed_at"], ts,
                            contracts, contracts * rules.contract_value, entry, d(candidate["stop_loss"]),
                            d(candidate["take_profit"]), contracts * margin_per_contract, contracts * fee_per_contract,
                            rules.maintenance_rate)
        if position.sign * (position.stop - position.liquidation()) <= 0:
            return "stop_beyond_liquidation"
        self.cash -= position.margin + position.entry_fee
        self.marks[instrument] = d(quote["mark"])
        self.positions[instrument] = position
        self.events.append({"at": ts, "event": "entry", "instrument": instrument, "price": entry,
                            "fee": position.entry_fee, "margin": position.margin, "contracts": contracts})
        return "entered"

    def fund(self, instrument, rate, mark, ts):
        p = self.positions[instrument]
        payment = -p.sign * d(rate) * d(mark) * p.base
        p.funding += payment
        self.events.append({"at": ts, "event": "funding", "instrument": instrument, "amount": payment, "price": d(mark), "rate": d(rate)})

    def close(self, instrument, reason, reference, ts, ambiguous=False, *, trigger_at=None, evidence="mark_1s"):
        p = self.positions.pop(instrument)
        reference = d(reference)
        if reason == "liquidation":
            # No venue-specific liquidation fill; forfeit the isolated balance.
            price = p.entry - p.sign * (p.margin + p.funding) / p.base
            fee, returned = ZERO, ZERO
        else:
            price = reference * (1 - p.sign * SLIPPAGE)
            tick = self.rules[instrument].tick
            price = (price / tick).to_integral_value(rounding=ROUND_FLOOR if p.sign == 1 else ROUND_CEILING) * tick
            fee = price * p.base * FEE_RATE
            returned = max(ZERO, p.margin + p.funding + p.pnl(price) - fee)
        self.cash += returned
        record = {
            "instrument": instrument, "side": p.side, "strategy": p.strategy, "signal_at": p.closed_at,
            "opened_at": p.opened_at, "closed_at": ts, "contracts": p.contracts, "base_quantity": p.base,
            "margin": p.margin, "entry": p.entry, "stop_loss": p.stop, "take_profit": p.target,
            "exit_reference": reference, "exit": price, "exit_reason": reason, "ambiguous_second": ambiguous,
            "trigger_at": trigger_at, "exit_evidence": evidence,
            "gross_pnl": p.pnl(price), "entry_fee": p.entry_fee, "exit_fee": fee,
            "funding": p.funding, "net_pnl": returned - p.margin - p.entry_fee,
        }
        self.trades.append(record)
        self.events.append({"at": ts, "event": "exit", **record})
        return record


def exit_trigger(position, trade, mark):
    """Bar is an observed time bucket. When order is unknown, take the adverse result."""
    long = position.sign == 1
    liq = position.liquidation()
    liquidation = mark and (d(mark[3]) <= liq if long else d(mark[2]) >= liq)
    stop = trade and (d(trade[3]) <= position.stop if long else d(trade[2]) >= position.stop)
    target = trade and (d(trade[2]) >= position.target if long else d(trade[3]) <= position.target)
    if not (liquidation or stop or target):
        return None
    if liquidation:
        return "liquidation", liq, bool(stop or target)
    opening = d(trade[1])
    if opening <= position.stop if long else opening >= position.stop:
        return "stop_loss", opening, False
    if opening >= position.target if long else opening <= position.target:
        return "take_profit", opening, False
    if stop:
        return "stop_loss", position.stop, bool(target)
    return "take_profit", position.target, False


def tick_exit(position, ticks):
    for ts, _, raw in ticks:
        price = d(raw)
        if position.sign * (price - position.stop) <= 0:
            return "stop_loss", price, ts
        if position.sign * (price - position.target) >= 0:
            return "take_profit", price, ts


async def settle_second(account, instrument, trade, mark, at, ticks):
    position = account.positions[instrument]
    if mark is not None:
        account.marks[instrument] = d(mark[4])
    trigger = exit_trigger(position, trade, mark)
    if trigger is None:
        return
    reason, reference, ambiguous = trigger
    if reason == "liquidation":
        account.close(instrument, reason, reference, at, ambiguous)
    else:
        observed = tick_exit(position, await ticks(instrument, trade[0]))
        if observed:
            reason, reference, ts = observed
            account.close(instrument, reason, reference, at, trigger_at=ts, evidence="ordered_trades")
        else:
            # Keep an explicitly identified OHLC estimate when tick and candle sources differ.
            account.close(instrument, reason, reference, at, ambiguous, evidence="trade_1s_unmatched")


async def process_second(account, trades, marks, entries, at, ticks):
    for inst in sorted(account.positions):
        await settle_second(account, inst, trades.get(inst), marks.get(inst), at, ticks)
    return [account.enter(row["instrument"], row["candidate"], row["verified_quote"], at)
            for row in sorted(entries, key=lambda row: row["instrument"])]
