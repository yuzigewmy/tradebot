"""Run the selected perpetual strategies on Binance's public feed."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager, closing

import aiohttp
import httpx

from .binance_execution import BinanceExecutor, TradeLedger, account_lock
from .binance_market import BinanceMarket
from .binance_sdk import MARGIN_PERCENT, BinanceFutures
from .perpetual_btc import BTC_INSTRUMENT, BTC_STRATEGY
from .perpetual_compression import COMPRESSION_INSTRUMENT, COMPRESSION_STRATEGY
from .perpetual_dual import DUAL_STRATEGY, dual_signal_candidate
from .perpetual_macro import (MACRO_A1_AGGRESSIVE, MACRO_A1_STEADY, MACRO_PROFILES,
                              MACRO_STRATEGIES, macro_a1_signal_candidate)
from .perpetual_market import HISTORY_LIMIT, MarketError
from .perpetual_monitor import PerpetualMonitor, SignalJournal
from .perpetual_regime_terms import LEVERAGE, fixed_signal_candidate
from .perpetual_router import (AUTO_REGIME_STRATEGY, ROUTER_S10_RISK_PERCENT,
                               auto_regime_signal_candidate)

STRATEGIES = {BTC_INSTRUMENT: AUTO_REGIME_STRATEGY, COMPRESSION_INSTRUMENT: AUTO_REGIME_STRATEGY}
ALLOWED_STRATEGIES = {
    BTC_INSTRUMENT: (AUTO_REGIME_STRATEGY, DUAL_STRATEGY, MACRO_A1_AGGRESSIVE, MACRO_A1_STEADY, BTC_STRATEGY),
    COMPRESSION_INSTRUMENT: (AUTO_REGIME_STRATEGY, DUAL_STRATEGY, MACRO_A1_AGGRESSIVE, MACRO_A1_STEADY, COMPRESSION_STRATEGY),
}


class BinanceMonitor(PerpetualMonitor):
    exchange = "Binance"

    def __init__(self, instrument, market, model, journal, settings, rules, ledger=None):
        super().__init__(instrument, market.client, model, journal)
        self.market, self.settings, self.rules, self.ledger = market, settings, rules, ledger
        self.selected_strategy = STRATEGIES[instrument]

    def start(self, mode, strategy="all"):
        wanted = STRATEGIES[self.instrument] if strategy == "all" else strategy
        if wanted not in ALLOWED_STRATEGIES[self.instrument]:
            raise ValueError("币安监控不支持该合约所选策略")
        super().start(mode, wanted)

    async def stream(self):
        await self.market.sync_clock(signing=self.settings.trading)
        self.tick = self.rules.tick
        clock = self.market.now()
        synced_at = clock
        self.skip_through = clock
        self.last_evaluated = 0
        self.bars, self.forming = {}, {}
        await self.refresh(clock - 1200, limit=HISTORY_LIMIT)
        self.last_quote = await self.get_quote()
        self.status, self.market_note = "online", "币安永续行情已连接"
        self.evaluate()
        while True:
            await asyncio.sleep(2)
            clock = self.market.now()
            if clock - synced_at >= 600_000:
                await self.market.sync_clock(signing=self.settings.trading)
                clock = self.market.now()
                synced_at = clock
            await self.refresh(clock - 1200, limit=3)
            self.evaluate()
            self.last_quote = await self.get_quote()

    def now(self):
        return self.market.now()

    async def refresh(self, as_of, *, limit):
        timeframes = tuple(self.channels.values())
        values = await asyncio.gather(*(self.market.candles(self.instrument, tf, as_of, limit=limit) for tf in timeframes))
        for tf, (closed, forming) in zip(timeframes, values, strict=True):
            if not closed:
                raise MarketError(f"币安 {tf} 无已收盘 K 线")
            if tf not in self.bars:
                self.bars[tf] = closed
            else:
                for row in closed:
                    self.ingest(tf, row, True)
            self.forming[tf] = forming

    async def get_quote(self, *, settled=False):
        return await self.market.quote(self.instrument)

    async def history(self, timeframe):
        return await self.market.candles(self.instrument, timeframe, await self.market.clock() - 1200)

    def fixed_candidate(self, policy, setup, market_quote, published_at):
        if policy.strategy == DUAL_STRATEGY:
            return dual_signal_candidate(self.instrument, setup, market_quote, self.rules, published_at, binance=True)
        if policy.strategy in MACRO_STRATEGIES:
            return macro_a1_signal_candidate(self.instrument, policy.strategy, setup, market_quote, self.rules, published_at, binance=True)
        if policy.strategy == AUTO_REGIME_STRATEGY:
            return auto_regime_signal_candidate(self.instrument, setup, market_quote, self.rules, published_at, binance=True)
        candidate, issue = fixed_signal_candidate(policy, setup, market_quote, self.rules, published_at, binance=True)
        if candidate is None:
            return None, issue
        return candidate | {"margin_percent": MARGIN_PERCENT, "leverage": int(LEVERAGE)}, None

    def previous_outcome(self, previous):
        if self.ledger is not None:
            return self.ledger.outcome(previous["id"])
        return {"status": "public_signal"}

    def publish_signal(self, signal, candidate, note):
        tagged = signal | {"exchange": self.exchange, "execution_mode": self.settings.mode}
        super().publish_signal(tagged, candidate, "币安策略信号已发布")

    def signal_is_active(self, signal, now):
        return (super().signal_is_active(signal, now)
                and (self.ledger is None or self.ledger.outcome(signal["id"]) is None))

    def view(self):
        result = super().view()
        if self.selected_strategy == AUTO_REGIME_STRATEGY:
            regime = (self.evaluation or {}).get("setup", {}).get("regime")
            lev = regime["active_leverage"] if regime else int(MACRO_PROFILES[MACRO_A1_AGGRESSIVE].leverage)
            risk = regime["active_risk_percent"] if regime else float(ROUTER_S10_RISK_PERCENT)
            result.update(execution_mode=self.settings.mode, risk_percent=risk, leverage=lev, margin_percent=None)
        elif self.selected_strategy in MACRO_PROFILES:
            profile = MACRO_PROFILES[self.selected_strategy]
            result.update(execution_mode=self.settings.mode, risk_percent=float(profile.risk_percent),
                          leverage=int(profile.leverage), margin_percent=None)
        else:
            result.update(execution_mode=self.settings.mode, margin_percent=MARGIN_PERCENT,
                          leverage=int(LEVERAGE), risk_percent=None)
        for signal in result["signals"]:
            signal["outcome"] = (self.ledger.status(signal["id"]) if self.ledger is not None else {"status": "public_signal"})
        return result


@asynccontextmanager
async def binance_monitors(directory, settings, on_signal, on_trade):
    async with (aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=True) as client,
                httpx.AsyncClient() as model):
        market = BinanceMarket(client, settings.mode)
        await market.sync_clock(signing=settings.trading)
        rules = await market.rules()
        journal_path = directory / f"binance-{settings.mode}-signals.jsonl"
        if not settings.trading:
            journal = SignalJournal(journal_path, on_signal)
            monitors = {inst: BinanceMonitor(inst, market, model, journal, settings, rules[inst]) for inst in STRATEGIES}
            try:
                yield monitors
            finally:
                await asyncio.gather(*(monitor.stop() for monitor in monitors.values()))
            return
        directory.mkdir(parents=True, exist_ok=True)
        with (account_lock(directory / f"binance-{settings.mode}.lock.sqlite"),
              closing(sqlite3.connect(directory / f"binance-{settings.mode}.sqlite")) as database):
            probe = BinanceFutures(settings)
            try:
                account = await probe.call("account")
            finally:
                probe.close_session()
            ledger = TradeLedger(database, account)
            executor = BinanceExecutor(settings, market, rules, ledger, on_trade, model)

            def publish(signal):
                executor.enqueue(signal)
                on_signal(signal)

            journal = SignalJournal(journal_path, publish, follow_trades=True)
            monitors = {inst: BinanceMonitor(inst, market, model, journal, settings, rules[inst], ledger) for inst in STRATEGIES}
            for signal, state in ledger.pending():
                executor.start(signal, state)
            try:
                yield monitors
            finally:
                await asyncio.gather(*(monitor.stop() for monitor in monitors.values()))
                await executor.close()
