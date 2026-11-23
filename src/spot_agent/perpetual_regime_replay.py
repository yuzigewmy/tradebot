"""Replay closed-bar strategies against official marked and traded prices."""

import asyncio
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, replace

import numpy as np

from .perpetual_history import MARK, MINUTE, SECOND, TRADE, load
from .perpetual_market import InstrumentRules, MarketError
from .perpetual_paper import FEE_RATE, SLIPPAGE, PaperAccount, d, exit_trigger, tick_exit
from .perpetual_regime import InstrumentFeatures, Signal
from .perpetual_regime_terms import CAPITAL, FUNDING_CHARGE, LEVERAGE, MARGIN, adverse_tick, holding_deadline, price_brackets

INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")


class Market:
    def __init__(self, folder, rules_file, start, end):
        raw_rules = load(rules_file)
        self.rules = {i: InstrumentRules(**{k: d(v) for k, v in r.items()}) for i, r in raw_rules.items()}
        start_with_mark = start - MINUTE
        self.start, self.end = start, end
        self.trade, self.mark, self.funding, self.features = {}, {}, {}, {}
        expected = np.arange(start_with_mark, end, MINUTE)
        for inst in INSTRUMENTS:
            trades = np.array(load(folder / f"{inst}-trades.json"), dtype=float)
            marks = np.array(load(folder / f"{inst}-marks.json"), dtype=float)
            self.features[inst] = InstrumentFeatures(trades)
            self.trade[inst] = trades[(trades[:, 0] >= start_with_mark) & (trades[:, 0] < end)]
            self.mark[inst] = marks[(marks[:, 0] >= start_with_mark) & (marks[:, 0] < end)]
            if not all(np.array_equal(s[inst][:, 0], expected) for s in (self.trade, self.mark)):
                raise MarketError(f"{inst} 成交或标记价格时段缺失")
            funding = dict(load(folder / f"{inst}-funding.json"))
            self.funding[inst] = {t: funding[t] for t in range(start, end, 8 * 60 * MINUTE)}

    def time(self, i):
        return self.start + (i - 1) * MINUTE

    def index(self, ts):
        return (ts - self.start) // MINUTE + 1

    def signals(self, variant):
        setup, brackets, symbols, sides = variant
        return [Signal(at, inst, setup.family, sign, atr, strength, brackets)
                for inst in symbols for at, sign, atr, strength in self.features[inst].select(setup)
                if self.start <= at and at + MINUTE < self.end and sign in sides]


@dataclass(frozen=True)
class Exit:
    instrument: str
    at: int
    reason: str
    price: float
    evidence: str
    ambiguous: bool = False
    trigger_at: int | None = None


def signal_entry(account, market, signal, idx, *, slip=SLIPPAGE, max_entry_drift=None, funding_buffer=0, book=None):
    inst = signal.instrument
    first = market.trade[inst][idx]
    if book is None:
        if first[5] == 0:
            return "no_trade"
        reference, depth = first[1], None
    else:
        bid, ask, bid_size, ask_size, _ = book
        reference, depth = (ask, ask_size) if signal.sign == 1 else (bid, bid_size)
    previous_close = market.trade[inst][idx - 2][4]
    if max_entry_drift is not None and abs(reference - previous_close) > max_entry_drift * signal.atr:
        return "entry_drift"
    entry = adverse_tick(reference, signal.sign, market.rules[inst].tick, slip)
    stop, target = price_brackets(entry, signal.sign, signal.atr, signal.bracket)
    entry_at = market.time(idx)
    exit_at = holding_deadline(entry_at, signal.bracket.hold, funding_buffer)
    if exit_at <= entry_at:
        return "funding_window"
    levels = {"side": "long" if signal.sign == 1 else "short", "closed_at": signal.at, "strategy": signal.strategy,
              "stop_loss": stop, "take_profit": target, "exit_at": exit_at}
    simulated_quote = {"bid": entry, "ask": entry, "bid_size": depth, "ask_size": depth, "mark": market.mark[inst][idx - 1][4]}
    return account.enter(inst, levels, simulated_quote, market.time(idx))


def active_stops(p, plan, trades, first, through):
    if not plan.bracket.trailing_atr:
        return np.full(through - first, float(p.stop))
    favorable = p.sign * trades[first:through, 2 if p.sign == 1 else 3]
    known_peaks = np.maximum.accumulate(np.r_[float(p.entry) * p.sign, favorable])[:-1]
    trail = known_peaks - plan.bracket.trailing_atr * plan.atr
    return p.sign * np.maximum(float(p.stop) * p.sign, trail)


class Tape:
    def __init__(self, history, verify_all=False):
        self.history = history
        self.verify_all = verify_all
        self.minutes = {}

    async def minute(self, inst, at):
        key = inst, at
        if key not in self.minutes:
            trade, mark = await asyncio.gather(
                self.history.candles(TRADE, inst, SECOND, at, at + MINUTE, sparse=True),
                self.history.candles(MARK, inst, SECOND, at, at + MINUTE, sparse=True),
            )
            self.minutes[key] = {row[0]: row for row in trade}, {row[0]: row for row in mark}
        return self.minutes[key]

    async def verify_entry(self, inst, at, opening):
        trades, _ = await self.minute(inst, at)
        second = min(t for t in trades if trades[t][5] > 0)
        executions = await self.history.trade_ticks(inst, second)
        if not executions or executions[0][2] != opening:
            raise MarketError(f"{inst} {at} 的分钟开盘价和第一笔成交不一致")
        return executions[0][0]

    async def refine(self, inst, position, at, entered=None):
        trades, marks = await self.minute(inst, at)
        start = at if entered is None else max(at, entered // SECOND * SECOND)
        for second in range(start, at + MINUTE, SECOND):
            trade, mark = trades.get(second), marks.get(second)
            if trade is not None and not trade[5]:
                trade = None
            touched = exit_trigger(position, trade, mark)
            overdue = trade is not None and second >= position.exit_at
            if touched and touched[0] == "liquidation":
                return Exit(inst, second, touched[0], float(touched[1]), "mark_1s", touched[2])
            if touched or overdue:
                ticks = await self.history.trade_ticks(inst, second)
                if entered is not None:
                    ticks = [tick for tick in ticks if tick[0] >= entered]
                execution = tick_exit(position, ticks)
                if execution:
                    reason, price, ts = execution
                    return Exit(inst, ts, reason, float(price), "ordered_trades")
                raise MarketError(f"{inst} {second} 的退出逐笔成交缺失")
        raise MarketError(f"{inst} {at} 的触价分钟无法用秒线重现")


def minute_close(inst, p, trade, mark, at):
    active_trade = trade.tolist() if trade[5] else None
    touched = exit_trigger(p, active_trade, mark.tolist())
    if touched and touched[0] == "liquidation":
        return Exit(inst, at, touched[0], float(touched[1]), "mark_1m", touched[2] or at >= p.exit_at)
    if at >= p.exit_at and active_trade is not None:
        result = tick_exit(p, ((at, 0, trade[1]),))
        return Exit(inst, at, result[0], float(result[1]), "trade_1m_open")
    if touched:
        return Exit(inst, at, touched[0], float(touched[1]), "trade_1m", touched[2])


def probe_exit(market, p, signal, first, ceiling, funding_charge):
    inst, sign = p.instrument, p.sign
    trades, marks = market.trade[inst], market.mark[inst]
    payments = sorted(market.funding[inst])
    p = replace(p)
    left = first
    last = min(ceiling, market.index(p.exit_at) + 60)
    stops = active_stops(p, signal, trades, first, last)
    while left < last:
        timestamp = market.time(left)
        split = bisect_right(payments, timestamp)
        right = min(last, market.index(payments[split])) if split < len(payments) else last
        trade, mark = trades[left:right], marks[left:right]
        stop = stops[left-first:right-first]
        loss = trade[:, 3 if sign == 1 else 2] * sign <= stop * sign
        profit = trade[:, 2 if sign == 1 else 3] * sign >= float(p.target) * sign
        liquidation = mark[:, 3 if sign == 1 else 2] * sign <= float(p.liquidation()) * sign
        timed = trade[:, 0] >= p.exit_at
        touched = np.flatnonzero(liquidation | ((loss | profit | timed) & (trade[:, 5] > 0)))
        if len(touched):
            i = left + int(touched[0])
            p.stop = d(stops[i-first])
            event = minute_close(inst, p, trades[i], marks[i], market.time(i))
            if event is None:
                raise RuntimeError("检测到触价但无法生成退出事件")
            return i, p, event
        left = right
        if left < last:
            rate = (sign * d(market.funding[inst][market.time(left)]) if funding_charge is None else funding_charge)
            p.funding -= rate * d(marks[left - 1, 4]) * p.base
    if last < ceiling:
        raise MarketError(f"{inst} 持仓到期后超过一小时仍未取得成交")
    return None


def exit_from_book(event, position, books, delay):
    if event.reason == "liquidation":
        return event
    executed = event.at + delay
    quote = books.get((event.instrument, executed))
    if quote is None:
        raise MarketError(f"{event.instrument} {executed} 缺少出场一档盘口")
    bid, ask, bid_size, ask_size, _ = quote[:5]
    price, top_size = (bid, bid_size) if position.sign == 1 else (ask, ask_size)
    if d(top_size) < position.contracts:
        if len(quote) == 5:
            raise MarketError(f"{event.instrument} {executed} 一档盘口无法覆盖出场数量")
        levels = quote[5 if position.sign == 1 else 6]
        remaining, total = position.contracts, d(0)
        for level_price, level_size in levels:
            amount = min(remaining, d(level_size))
            remaining -= amount
            total += amount * d(level_price)
            if remaining == 0:
                break
        if remaining > 0:
            raise MarketError(f"{event.instrument} {executed} 历史盘口无法覆盖出场数量")
        price = float(total / position.contracts)
    return replace(event, at=executed, price=price, evidence="ordered_trigger_l2_exit", trigger_at=event.at)


async def replay(market, signals, *, tape=None, funding_charge=FUNDING_CHARGE, maximum_positions=2,
                 start=None, end=None, collect=True, max_entry_drift=None, cooldown=0, funding_buffer=0,
                 entry_slippage=SLIPPAGE, books=None, exit_books=None, exit_delay_ms=0):
    start = market.start if start is None else start
    end = market.end if end is None else end
    begin, ceiling = market.index(start), market.index(end)
    account = PaperAccount(market.rules, cash=CAPITAL, leverage=LEVERAGE, fixed_margin=MARGIN, funding_charge=funding_charge)
    orders = defaultdict(list)
    for r in signals:
        if start <= r.at and r.at + MINUTE < end:
            orders[market.index(r.at + MINUTE)].append(r)
    exits = {}
    by_minute = defaultdict(dict)
    for inst in INSTRUMENTS:
        for at, rate in market.funding[inst].items():
            if start <= at < end:
                by_minute[market.index(at)][inst] = rate
    timetable = sorted(orders.keys() | by_minute.keys() | {ceiling})
    equity_path = np.full(ceiling - begin, float(CAPITAL)) if collect else None
    diff = np.zeros(ceiling - begin + 1) if collect else None
    rejected = Counter()
    entry_verified = book_verified = 0
    last_order = {}
    for step in timetable:
        for inst, event in sorted(tuple(exits.items()), key=lambda value: value[1].at):
            if event.at < market.time(step):
                completed = account.close(inst, event.reason, event.price, event.at, event.ambiguous,
                                          trigger_at=event.at if event.trigger_at is None else event.trigger_at, evidence=event.evidence)
                if collect:
                    j = market.index(event.at) - begin
                    diff[j] += float(completed["margin"] + completed["entry_fee"] + completed["net_pnl"])
                del exits[inst]
        for inst, rate in by_minute.get(step, {}).items():
            if inst in account.positions:
                before = account.positions[inst].funding
                account.fund(inst, rate, market.mark[inst][step - 1, 4], market.time(step))
                if collect:
                    # Funding adjusts isolated value instead of free cash.
                    change = float(account.positions[inst].funding - before)
                    closing = market.index(exits[inst].at) if inst in exits else ceiling
                    equity_path[step-begin:closing-begin] += change
        # Exit at the first observed minute trade precedes entries in that minute.
        for inst, event in sorted(tuple(exits.items())):
            if event.at == market.time(step):
                completed = account.close(inst, event.reason, event.price, event.at, event.ambiguous,
                                          trigger_at=event.at if event.trigger_at is None else event.trigger_at, evidence=event.evidence)
                if collect:
                    diff[step-begin] += float(completed["margin"] + completed["entry_fee"] + completed["net_pnl"])
                del exits[inst]
        if step == ceiling:
            break
        for r in sorted(orders.get(step, []), key=lambda item: (-item.strength, item.instrument, item.strategy)):
            if r.instrument in account.positions:
                rejected["instrument_position_open"] += 1
                continue
            if len(account.positions) >= maximum_positions:
                rejected["portfolio_position_limit"] += 1
                continue
            if r.at - last_order.get(r.instrument, -10**16) < cooldown * MINUTE:
                rejected["cooldown"] += 1
                continue
            book = None if books is None else books.get((r.instrument, market.time(step)))
            if books is not None and book is None:
                rejected["missing_book"] += 1
                continue
            state = signal_entry(account, market, r, step, slip=entry_slippage, max_entry_drift=max_entry_drift,
                                 funding_buffer=funding_buffer, book=book)
            if state != "entered":
                rejected[state] += 1
                continue
            last_order[r.instrument] = r.at
            p = account.positions[r.instrument]
            entered = None
            if book is not None:
                book_verified += 1
            elif tape is not None and tape.verify_all:
                entered = await tape.verify_entry(r.instrument, market.time(step), market.trade[r.instrument][step, 1])
                p.opened_at = entered
                entry_verified += 1
            outcome = probe_exit(market, p, r, step, ceiling, funding_charge)
            closing_index = ceiling
            if outcome:
                closing_index, levels, event = outcome
                if tape is not None and (event.ambiguous or tape.verify_all):
                    event = await tape.refine(r.instrument, levels, event.at, entered=entered if closing_index == step else None)
                if exit_books is not None:
                    event = exit_from_book(event, p, exit_books, exit_delay_ms)
                    closing_index = min(ceiling, market.index(event.at))
                exits[r.instrument] = event
            if collect:
                diff[step-begin] -= float(p.margin + p.entry_fee)
                held = market.mark[r.instrument][step:closing_index, 4]
                equity_path[step-begin:closing_index-begin] += float(p.margin) + p.sign * (held - float(p.entry)) * float(p.base)
    for inst in account.positions:
        account.marks[inst] = d(market.mark[inst][ceiling-1, 4])
    final = account.equity()
    pnl = [t["net_pnl"] for t in account.trades]
    wins, count = sum(p > 0 for p in pnl), len(pnl)
    result = {"ending_equity": float(final), "return_pct": float((final / CAPITAL - 1) * 100), "trades": count,
              "wins": wins, "win_pct": 100 * wins / count if count else 0,
              "funding_paid_net": float(-sum(e["amount"] for e in account.events if e["event"] == "funding")),
              "fees": float(sum(e["fee"] for e in account.events if e["event"] == "entry") + sum(t["exit_fee"] for t in account.trades)),
              "start": start, "end": end, "open_positions": len(account.positions), "skipped": dict(rejected),
              "entry_first_trade_verified": entry_verified, "entry_book_depth_verified": book_verified,
              "exit_delay_ms": exit_delay_ms if exit_books is not None else None,
              "reasons": dict(Counter(t["exit_reason"] for t in account.trades)),
              "evidence": dict(Counter(t["exit_evidence"] for t in account.trades)),
              "ambiguous_minutes": sum(bool(t["ambiguous_second"]) for t in account.trades)}
    if collect:
        equity = np.r_[float(CAPITAL), (equity_path + np.cumsum(diff[:-1]))[:-1], float(final)]
        peak = np.maximum.accumulate(equity)
        result["max_drawdown_pct"] = float(((peak-equity)/peak).max() * 100)
        result["min_equity"] = float(equity.min())
        return result, account.trades, equity
    return result
