"""Resolve each signal independently from OKX's ordered executions."""

import asyncio
from dataclasses import dataclass
from decimal import Decimal
import logging

import aiohttp

from .perpetual_history import MINUTE, PAGE, SECOND, TRADE
from .perpetual_market import MarketError, now_ms
from .perpetual_paper import d, tick_exit

logger = logging.getLogger(__name__)


def entry_second(record):
    return (record["published_at"] + SECOND - 1) // SECOND * SECOND


@dataclass(frozen=True)
class ExitLevels:
    sign: int
    stop: Decimal
    target: Decimal
    exit_at: int | None

    def may_exit(self, bar, step):
        at, _, high, low, _, volume = bar
        return volume > 0 and (
            self.sign * (d(low if self.sign == 1 else high) - self.stop) <= 0
            or self.sign * (d(high if self.sign == 1 else low) - self.target) >= 0
            or (self.exit_at is not None and self.exit_at < at + step)
        )


class SignalOutcomes:
    def __init__(self, history, journal):
        self.history, self.journal = history, journal
        self.cursors = {}

    async def run(self, *, skip_assigned=False):
        while True:
            end = (now_ms() - 2 * SECOND) // MINUTE * MINUTE
            for record in tuple(self.journal.records):
                if not skip_assigned or "trade_agent_id" not in record:
                    await self.update(record, end)
            await asyncio.sleep(10)

    async def update(self, record, end):
        signal_id = record["id"]
        current = self.journal.outcomes.get(signal_id)
        if current is not None and current["status"] != "unverified":
            return
        entered = entry_second(record)
        if entered >= record["expires_at"]:
            self.journal.save_outcome(signal_id, {"status": "entry_expired"})
            return
        start = self.cursors.get(signal_id, entered // MINUTE * MINUTE)
        end = min(end, start + PAGE * MINUTE)
        if end <= start:
            return
        levels = ExitLevels(1 if record["side"] == "long" else -1, d(record["stop_loss"]),
                            d(record["take_profit"]), record.get("exit_at"))
        try:
            instrument = record["instrument"]
            minutes = await self.history.candles(TRADE, instrument, MINUTE, start, end)
            result = await self.scan(instrument, levels, entered, minutes)
        except (aiohttp.ClientError, MarketError, TimeoutError) as error:
            if current is None:
                logger.warning("信号 %s 的成交证据暂缺（%s）", signal_id, type(error).__name__)
            self.journal.outcomes[signal_id] = {"status": "unverified"}
            return
        if result is not None:
            status, price, at = result
            self.journal.save_outcome(signal_id, {"status": status, "at": at, "price": float(price)})
            self.cursors.pop(signal_id, None)
        else:
            self.cursors[signal_id] = end
            self.journal.outcomes.pop(signal_id, None)
            return minutes

    async def scan(self, instrument, levels, entered, minutes):
        for minute in minutes:
            if not levels.may_exit(minute, MINUTE):
                continue
            seconds = await self.history.candles(TRADE, instrument, SECOND, minute[0], minute[0] + MINUTE)
            candidates = [row[0] for row in seconds if levels.may_exit(row, SECOND)]
            if not candidates:
                raise MarketError("分钟与秒级成交记录不一致")
            for at in candidates:
                if at < entered:
                    continue
                if result := tick_exit(levels, await self.history.trade_ticks(instrument, at)):
                    return result
                raise MarketError("未取得与秒线对应的逐笔退出证据")
