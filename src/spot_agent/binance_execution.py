"""Persisted Binance order lifecycle. Exchange fills, not candles, settle a trade."""

import asyncio
from contextlib import closing, suppress
from decimal import Decimal
import hashlib
import json
import logging
import sqlite3

import aiohttp

from .agent import model_configured
from .binance_market import SYMBOLS
from .contracts import TIMEFRAMES
from .binance_sdk import BinanceError, BinanceFutures
from .perpetual_market import MarketError, now_ms
from .perpetual_paper import d
from .perpetual_regime_terms import LEVERAGE
from .perpetual_trade import REVIEW_ERRORS, TradeAgent, TradeNotice, time_label

log = logging.getLogger(__name__)
ORDER_DONE = {"FILLED", "EXPIRED", "EXPIRED_IN_MATCH", "CANCELED", "REJECTED"}
ALGO_DONE = {"CANCELED", "EXPIRED", "FINISHED", "REJECTED"}


class TradeLedger:
    def __init__(self, database, account):
        self.database = database
        database.executescript("""
            CREATE TABLE IF NOT EXISTS account (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, instrument TEXT NOT NULL, signal TEXT NOT NULL,
                state TEXT NOT NULL, outcome TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_trade ON trades (instrument) WHERE outcome IS NULL;
        """)
        with database:
            rows = database.execute("SELECT id FROM account").fetchall()
            if not rows:
                database.execute("INSERT INTO account VALUES (?)", (account,))
            elif rows != [(account,)]:
                raise BinanceError("币安执行账本属于另一个账户")

    def add(self, signal):
        with self.database:
            result = self.database.execute("INSERT OR IGNORE INTO trades VALUES (?, ?, ?, '{}', NULL)",
                                           (signal["id"], signal["instrument"], json.dumps(signal)))
        return result.rowcount == 1

    def pending(self):
        for signal, state in self.database.execute("SELECT signal, state FROM trades WHERE outcome IS NULL"):
            yield json.loads(signal), json.loads(state)

    def save(self, signal_id, state, outcome=None):
        with self.database:
            self.database.execute("UPDATE trades SET state=?, outcome=? WHERE id=?",
                                  (json.dumps(state), json.dumps(outcome) if outcome else None, signal_id))

    def status(self, signal_id):
        row = self.database.execute("SELECT state, outcome FROM trades WHERE id=?", (signal_id,)).fetchone()
        if row is None:
            return {"status": "entry_skipped", "verdict": "失败"}
        state, outcome = row
        progress = json.loads(state)
        sizing = {}
        if "margin_budget" in progress:
            sizing["margin_budget"] = float(progress["margin_budget"])
        if outcome:
            return json.loads(outcome) | sizing
        status = "pending"
        if progress.get("close_reason"):
            status = "closing"
        elif progress.get("protected"):
            status = "protected"
        elif progress.get("entry_sent"):
            status = "entry_ordered"
        return {"status": status, **sizing}

    def outcome(self, signal_id):
        status = self.status(signal_id)
        return status if "verdict" in status else None


class BinancePositionReview(TradeAgent):
    def now(self):
        return self.market.now()

    async def get_quote(self):
        return await self.market.quote(self.instrument)

    async def recent_candles(self):
        closed, forming = await self.market.candles(self.instrument, "1m", self.market.now() - 1200, limit=3)
        return closed + ([forming] if forming is not None else [])

    async def run(self):
        checked = self.entry_at
        while True:
            await asyncio.sleep(5)
            try:
                closed, _ = await self.market.candles(self.instrument, "1m", self.market.now() - 1200, limit=10)
                rows = [row for row in closed if row[0] >= checked]
                if not rows:
                    continue
                checked = rows[-1][0] + TIMEFRAMES["1m"]
                prospect = self.observe(rows)
                if prospect and prospect.kind not in self.notified:
                    await self.advise(prospect)
            except REVIEW_ERRORS as exc:
                log.warning("币安持仓子 agent %s 提前退出复核未完成（%s）", self.signal["trade_agent_id"], type(exc).__name__)


class BinanceTrade:
    def __init__(self, signal, state, ledger, sdk, market, rules, notify, mode, model=None):
        self.signal, self.state, self.ledger = signal, state, ledger
        self.sdk, self.market, self.rules, self.notify = sdk, market, rules, notify
        self.mode, self.model = mode, model
        self.advice = None
        self.symbol = SYMBOLS[signal["instrument"]]
        self.long = signal["side"] == "long"
        digest = hashlib.sha256(signal["id"].encode()).hexdigest()[:23]
        self.prefix = f"tb-{digest}"
        self.entry_id = f"{self.prefix}-entry"
        self.protections = {leg: f"{self.prefix}-{leg}" for leg in ("stop", "target")}
        self.label = f"币安{' Demo' if mode == 'demo' else '实盘'} {self.symbol} {'做多' if self.long else '做空'}"

    def save(self, **fields):
        self.state.update(fields)
        self.ledger.save(self.signal["id"], self.state)

    def notice(self, kind, verdict, body):
        self.notify(TradeNotice(self.signal["trade_agent_id"], kind, self.signal["instrument"], verdict,
                                f"{self.label}，{body}"))

    def finish(self, outcome, verdict, body):
        self.notice("closed", verdict, body)
        self.ledger.save(self.signal["id"], self.state, outcome | {"verdict": verdict})
        return True

    async def run(self):
        try:
            await self.track()
        finally:
            await self.stop_advice()
            if hasattr(self.sdk, "close_session"):
                self.sdk.close_session()

    async def stop_advice(self):
        if self.advice is not None:
            self.advice.cancel()
            with suppress(asyncio.CancelledError):
                await self.advice
            self.advice = None

    async def track(self):
        while True:
            try:
                if await self.step():
                    return
                delay = 2
            except (BinanceError, MarketError, aiohttp.ClientError, TimeoutError) as exc:
                reason = str(exc) if isinstance(exc, (BinanceError, MarketError)) else type(exc).__name__
                if reason != self.state.get("last_error"):
                    log.warning("币安子 agent %s 待核对（%s）", self.signal["trade_agent_id"], reason)
                    self.notice("order_check", "失败", f"订单状态待核对（{reason}），按原订单编号继续查询。")
                    self.save(last_error=reason)
                delay = (exc.retry_after or 2) if isinstance(exc, BinanceError) else 2
            await asyncio.sleep(delay)

    async def step(self):
        if "entry_sent" not in self.state:
            return await self.submit_entry()
        if "entry" not in self.state:
            order = await self.sdk.call("order", self.symbol, self.entry_id)
            if order is None:
                if self.market.now() >= self.signal["expires_at"] + 5000 and not await self.sdk.call("position", self.symbol):
                    return self.finish({"status": "entry_unfilled"}, "失败", "入场窗结束，币安未查到成交。")
                return
            self.save(entry=order)
        order = self.state["entry"]
        if order["status"] not in ORDER_DONE:
            order = await self.sdk.call("order", self.symbol, self.entry_id)
            if order is not None:
                self.save(entry=order)
            return
        quantity = d(order["executedQty"])
        if not quantity:
            return self.finish({"status": "entry_unfilled"}, "失败", "限价单未成交，入场结束。")
        position = await self.sdk.call("position", self.symbol)
        if position is None:
            await self.stop_advice()
            return await self.settle(order, quantity)
        position_qty = d(position["positionAmt"])
        if (position_qty > 0) != self.long or abs(position_qty) > quantity:
            raise BinanceError("币安仓位与本次成交不符，请检查账户是否有其他交易")
        if self.state.get("close_reason") or self.market.now() >= self.signal["exit_at"]:
            return await self.close_position(abs(position_qty), self.state.get("close_reason", "到达策略持仓截止时间"))
        stop, liquidation = d(self.signal["stop_loss"]), d(position["liquidationPrice"])
        if liquidation > 0 and (stop <= liquidation if self.long else stop >= liquidation):
            return await self.close_position(abs(position_qty), "实际强平价已越过计划止损")
        await self.protect(position, abs(position_qty))
        if (self.state.get("protected") and not self.state.get("close_reason")
                and self.advice is None and self.model is not None and model_configured()):
            record = self.signal | {"entry": float(position["entryPrice"]), "published_at": order["updateTime"],
                                    "execution_label": self.label}
            worker = BinancePositionReview(record, None, self.market, self.model, self.notify)
            self.advice = asyncio.create_task(worker.run(), name=self.signal["trade_agent_id"] + "-review")

    async def submit_entry(self):
        if self.market.now() >= self.signal["expires_at"]:
            return self.finish({"status": "entry_expired"}, "失败", "已过入场窗口。")
        try:
            margin_budget = await self.sdk.call("prepare", self.symbol)
        except BinanceError as exc:
            if exc.unknown or exc.retry_after is not None:
                raise
            return self.finish({"status": "entry_rejected"}, "失败", f"下单前核验失败（{exc}）。")
        quote = await self.market.quote(self.signal["instrument"])
        limit = d(self.signal["entry"])
        bid, ask, last = (d(quote[key]) for key in ("bid", "ask", "last"))
        low, high = sorted((d(self.signal["stop_loss"]), d(self.signal["take_profit"])))
        size_price = max(limit, ask, d(quote["mark"]))
        quantity = self.rules.quantity(limit, margin_budget * LEVERAGE, sizing_price=size_price)
        now = self.market.now()
        remaining = min(5000, self.signal["expires_at"] - now - 800)
        if remaining <= 0 or now >= self.signal["expires_at"] - 500:
            return self.finish({"status": "entry_expired"}, "失败", "报价核验结束时已过入场窗口。")
        if quantity is None:
            return self.finish({"status": "entry_rejected"}, "失败",
                               f"{margin_budget:f} USDT 保证金预算对应的仓位不符合币安数量或最小名义额限制。")
        book_quantity = d(quote["ask_size" if self.long else "bid_size"])
        if (book_quantity < quantity or not low < last < high
                or (ask > limit if self.long else bid < limit)
                or not -500 <= self.market.now() - quote["ticker_at"] <= 2000):
            return self.finish({"status": "entry_unfilled"}, "失败", "当前币安盘口无法按计划限价入场。")
        self.save(entry_sent=self.market.now(), margin_budget=f"{margin_budget:f}")
        try:
            order = await self.sdk.call("enter", self.symbol, self.long, limit, quantity, self.entry_id, remaining)
        except BinanceError as exc:
            if exc.unknown:
                raise
            return self.finish({"status": "entry_rejected"}, "失败", f"币安拒绝开仓（{exc}）。")
        self.save(entry=order)
        return await self.step()

    async def protect(self, position, quantity):
        assigned = self.state.get("brackets", {})
        for leg, client_id in self.protections.items():
            previous = assigned.get(leg)
            if previous is None:
                assigned = assigned | {leg: now_ms()}
                self.save(brackets=assigned)
                trigger = d(self.signal["stop_loss" if leg == "stop" else "take_profit"])
                try:
                    order = await self.sdk.call("protect", self.symbol, self.long, trigger, leg, client_id)
                except BinanceError as exc:
                    if exc.unknown:
                        raise
                    return await self.close_position(quantity, f"{('止损' if leg == 'stop' else '止盈')}挂单被拒绝（{exc}）")
            else:
                order = await self.sdk.call("algo", client_id)
            if order is not None and order["algoStatus"] in {"TRIGGERING", "TRIGGERED"}:
                if self.market.now() - order["updateTime"] < 5000:
                    return
            if order is None and previous is not None and now_ms() - previous < 6000:
                return
            if order is None or order["algoStatus"] != "NEW":
                return await self.close_position(quantity, f"{('止损' if leg == 'stop' else '止盈')}单已结束或无法核实，仍有剩余仓位")
        if not self.state.get("protected"):
            budget = self.state.get("margin_budget")
            sizing = f"保证金预算 {budget} USDT。" if budget is not None else ""
            self.notice("protected", "成功", f"实际开仓均价 {position['entryPrice']} USDT，数量 {quantity}。{sizing}"
                        f"止盈 {self.signal['take_profit']}，止损 {self.signal['stop_loss']}，保护单均已挂好。")
            self.save(protected=True)

    async def close_position(self, quantity, reason):
        await self.stop_advice()
        if not self.state.get("close_reason"):
            self.save(close_reason=reason)
            if reason != "到达策略持仓截止时间":
                self.notice("protection_issue", "失败", f"{reason}，执行只减仓退出。")
        attempt = self.state.get("exit_attempt", 0)
        if attempt:
            previous = await self.sdk.call("order", self.symbol, f"{self.prefix}-exit-{attempt}")
            if previous is None and now_ms() - self.state["exit_sent"] < 6500:
                return
            if previous is not None and (previous["status"] not in ORDER_DONE or now_ms() - self.state["exit_sent"] < 2500):
                return
        attempt += 1
        self.save(exit_attempt=attempt, exit_sent=now_ms())
        try:
            await self.sdk.call("close", self.symbol, self.long, quantity, f"{self.prefix}-exit-{attempt}")
        except BinanceError as exc:
            if exc.code == -2022:
                # Binance can reject a reduce-only order that conflicts with existing exits.
                for leg, client_id in self.protections.items():
                    if leg in self.state.get("brackets", {}):
                        await self.sdk.call("cancel", client_id)
            raise

    async def settle(self, order, quantity):
        start = self.state["entry_sent"] - 1500
        fills = await self.sdk.call("fills", self.symbol, start, self.market.now())
        entries = [fill for fill in fills if fill["orderId"] == order["orderId"]]
        if sum(d(fill["qty"]) for fill in entries) != quantity:
            return
        opened_at = min(fill["time"] for fill in entries)
        closes = [fill for fill in fills if fill["orderId"] != order["orderId"]
                  and fill["time"] >= opened_at and fill["side"] == ("SELL" if self.long else "BUY")]
        if sum(d(fill["qty"]) for fill in closes) != quantity:
            return
        triggered = {}
        for leg, client_id in self.protections.items():
            if leg not in self.state.get("brackets", {}):
                continue
            algo = await self.sdk.call("algo", client_id)
            if algo is None:
                if now_ms() - self.state["brackets"][leg] < 6000:
                    return
            else:
                if algo["algoStatus"] in {"TRIGGERING", "TRIGGERED"}:
                    return
                if algo["algoStatus"] not in ALGO_DONE:
                    await self.sdk.call("cancel", client_id)
                    return
                if algo.get("actualOrderId") not in (None, "", "0"):
                    triggered[int(algo["actualOrderId"])] = leg
        entry_price = sum(d(fill["price"]) * d(fill["qty"]) for fill in entries) / quantity
        exit_price = sum(d(fill["price"]) * d(fill["qty"]) for fill in closes) / quantity
        ended = max(fill["time"] for fill in closes)
        pnl = sum(d(fill["realizedPnl"]) for fill in closes)
        fees = {}
        for fill in entries + closes:
            asset = fill["commissionAsset"]
            fees[asset] = fees.get(asset, Decimal(0)) + d(fill["commission"])
        pnl -= fees.pop("USDT", Decimal(0))
        if self.signal["verified_quote"]["funding_at"] <= ended:
            income = await self.sdk.call("funding", self.symbol, start, ended)
            pnl += sum(d(item["income"]) for item in income if item["asset"] == "USDT")
        extra = "，另收 " + "、".join(f"{value:f} {asset}" for asset, value in fees.items()) if fees else ""
        last = max(closes, key=lambda fill: fill["time"])
        leg = triggered.get(last["orderId"])
        reason = {"stop": "止损触发", "target": "止盈触发"}.get(leg, self.state.get("close_reason", "币安仓位已归零"))
        return self.finish({"status": "binance_closed", "at": ended, "price": float(exit_price), "pnl_usdt": float(pnl)},
                           "成功" if pnl > 0 else "失败", f"{reason}，北京时间 {time_label(ended)}。"
                           f"开仓均价 {entry_price:f}，平仓均价 {exit_price:f}，数量 {quantity:f}，"
                           f"USDT 净盈亏 {pnl:+f}{extra}。")


class BinanceExecutor:
    def __init__(self, settings, market, rules, ledger, notify, model):
        self.settings, self.market, self.rules, self.ledger, self.notify = settings, market, rules, ledger, notify
        self.model = model
        self.tasks = set()

    def start(self, signal, state):
        trade = BinanceTrade(signal, state, self.ledger, BinanceFutures(self.settings), self.market,
                             self.rules[signal["instrument"]], self.notify, self.settings.mode, self.model)
        task = asyncio.create_task(trade.run(), name=signal["trade_agent_id"])
        self.tasks.add(task)
        task.add_done_callback(self.done)

    def done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("币安持仓子 agent %s 意外停止", task.get_name(), exc_info=task.exception())

    def enqueue(self, signal):
        if self.ledger.add(signal):
            self.start(signal, {})
        else:
            log.info("币安交易信号 %s 已处理或该合约已有受管仓位", signal["id"])

    async def close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


def account_lock(path):
    connection = sqlite3.connect(path, timeout=0)
    try:
        connection.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError:
        connection.close()
        raise BinanceError("同一币安环境的下单执行器已在运行") from None
    return closing(connection)
