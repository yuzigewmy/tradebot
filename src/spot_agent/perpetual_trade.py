"""One independent review worker per simulated perpetual entry."""

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from typing import Literal
from zoneinfo import ZoneInfo

import aiohttp
import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from .agent import AgentError, model_configured, response_text, responses_config
from .perpetual_history import MINUTE, SECOND
from .perpetual_market import MarketError, now_ms, parse_candle, public_data, quote
from .perpetual_outcomes import entry_second
from .perpetual_paper import FEE_RATE, SLIPPAGE, d
from .perpetual_strategy import SKILL_PATH, quote_issue

logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
REVIEW_SKILL = (SKILL_PATH / "position-review.md").read_text(encoding="utf-8")
REVIEW_ERRORS = (aiohttp.ClientError, httpx.HTTPError, MarketError, AgentError, TimeoutError, ValidationError, json.JSONDecodeError)


@dataclass(frozen=True)
class TradeNotice:
    agent_id: str
    kind: str
    instrument: str
    result: str
    body: str
    expires_at: int | None = None

    @property
    def key(self):
        return f"{self.agent_id}:{self.kind}"

    @property
    def text(self):
        return f"{self.agent_id}\n{self.result}\n{self.body}"


@dataclass(frozen=True)
class EarlyExit:
    kind: str
    closed_at: int
    close: float


class PositionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["early_take_profit", "early_stop_loss", "hold"]


def close_return(entry, reference, sign):
    exit_price = reference * (1 - sign * SLIPPAGE)
    return (sign * (exit_price - entry) - FEE_RATE * (exit_price + entry)) / entry


def time_label(at):
    return datetime.fromtimestamp(at / SECOND, BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def position_label(signal):
    side = "做多" if signal["side"] == "long" else "做空"
    return f"{signal['instrument']} {side}"


def closed_notice(signal, outcome):
    status = outcome["status"]
    sign = 1 if signal["side"] == "long" else -1
    net = close_return(d(signal["entry"]), d(outcome["price"]), sign)
    win = status == "take_profit" or (status == "time_limit" and net > 0)
    trigger = {"take_profit": "触及止盈", "stop_loss": "触及止损", "time_limit": "到期退出"}[status]
    body = (f"{position_label(signal)} 模拟结案，{trigger}。北京时间 {time_label(outcome['at'])}，"
            f"开仓参考 {signal['entry']}，触发成交价 {outcome['price']} USDT，"
            f"估算名义收益率 {net:+.3%}（计手续费和滑点，资金费未计）。")
    return TradeNotice(signal["trade_agent_id"], "closed", signal["instrument"], "成功" if win else "失败", body)


class TradeAgent:
    def __init__(self, signal, outcomes, market, model, notify):
        self.signal, self.outcomes, self.market, self.model, self.notify = signal, outcomes, market, model, notify
        self.instrument = signal["instrument"]
        self.sign = 1 if signal["side"] == "long" else -1
        self.entry, self.stop, self.target = (d(signal[key]) for key in ("entry", "stop_loss", "take_profit"))
        self.entry_at, self.cutoff = entry_second(signal), signal.get("exit_at")
        self.risk = abs(self.entry - self.stop)
        self.anchor = d(signal["event"]["setup"]["stop_anchor"])
        self.peak, self.structure_breaks = d(0), 0
        self.recent = deque(maxlen=5)
        self.notified = set()

    def observe(self, rows):
        prospect = None
        for row in rows:
            start, _, high, low, close, volume = row
            if start < self.entry_at:
                continue
            closed_at = start + MINUTE
            if self.cutoff is not None and closed_at >= self.cutoff:
                return
            if volume == 0:
                self.structure_breaks = 0
                prospect = None
                continue
            self.recent.append(row)
            price = d(close)
            self.peak = max(self.peak, self.sign * (d(high if self.sign == 1 else low) - self.entry))
            self.structure_breaks = self.structure_breaks + 1 if self.sign * (price - self.anchor) < 0 else 0
            prospect = self.decision(price, closed_at)
        return prospect

    def decision(self, price, closed_at):
        move = self.sign * (price - self.entry)
        if self.peak >= self.risk and self.peak - move >= self.risk / 2 and close_return(self.entry, price, self.sign) > 0:
            return EarlyExit("early_take_profit", closed_at, float(price))
        if self.structure_breaks >= 2 and move <= -self.risk / 2 and self.sign * (price - self.anchor) < 0:
            return EarlyExit("early_stop_loss", closed_at, float(price))

    async def run(self):
        while True:
            now = now_ms()
            end = (now - 2 * SECOND) // MINUTE * MINUTE
            rows = await self.outcomes.update(self.signal, end)
            result = self.outcomes.journal.outcomes.get(self.signal["id"])
            if result is not None and result["status"] != "unverified":
                self.notify(closed_notice(self.signal, result))
                return
            if rows:
                prospect = self.observe(rows)
                if prospect and prospect.kind not in self.notified and model_configured():
                    try:
                        await self.advise(prospect)
                    except REVIEW_ERRORS as error:
                        logger.warning("持仓子 agent %s 提前退出复核未完成（%s）", self.signal["trade_agent_id"], type(error).__name__)
            await asyncio.sleep(5)

    def now(self):
        return now_ms()

    async def get_quote(self):
        return await quote(self.market, self.instrument)

    async def recent_candles(self):
        rows = await public_data(self.market, "market/candles", instId=self.instrument, bar="1m", limit=2)
        return [parse_candle(row)[0] for row in rows]

    async def advise(self, prospect):
        expires = prospect.closed_at + MINUTE if self.cutoff is None else min(prospect.closed_at + MINUTE, self.cutoff)
        if self.now() >= expires:
            return
        market_quote = await self.get_quote()
        if self.revalidate(prospect, market_quote, expires) is None:
            return
        review = await review_position(self.model, {
            "agent_id": self.signal["trade_agent_id"], "instrument": self.instrument, "side": self.signal["side"],
            "entry": float(self.entry), "stop": float(self.stop), "target": float(self.target),
            "risk": float(self.risk), "peak_move": float(self.peak), "structure_anchor": float(self.anchor),
            "consecutive_structure_breaks": self.structure_breaks, "closed_at": prospect.closed_at,
            "close": prospect.close, "proposed_action": prospect.kind, "quote": market_quote,
            "recent_closed_candles": list(self.recent),
        })
        if review.decision != prospect.kind or self.now() >= expires:
            return
        market_quote, candles = await asyncio.gather(self.get_quote(), self.recent_candles())
        bottom, top = sorted((self.stop, self.target))
        for bar in candles:
            if bar[0] >= prospect.closed_at and (d(bar[3]) <= bottom or d(bar[2]) >= top):
                return
        if (price := self.revalidate(prospect, market_quote, expires)) is None:
            return
        profit = prospect.kind == "early_take_profit"
        action = "提前止盈" if profit else "提前止损"
        if profit:
            giveback = (self.peak - self.sign * (price - self.entry)) / self.risk
            evidence = f"最大有利变动 {self.peak / self.risk:.2f} R，已回吐 {giveback:.2f} R"
        else:
            evidence = f"连续 {self.structure_breaks} 根一分钟收盘失守入场结构 {self.anchor} USDT"
        body = (f"{self.signal.get('execution_label', position_label(self.signal))} 建议{action}，持仓仍在追踪。{evidence}。"
                f"当前平仓参考 {price} USDT，原止盈 {self.target}，原止损 {self.stop}。")
        self.notify(TradeNotice(self.signal["trade_agent_id"], prospect.kind, self.instrument,
                                "成功" if profit else "失败", body, expires))
        self.notified.add(prospect.kind)

    def revalidate(self, prospect, market_quote, expires):
        now = self.now()
        if now >= expires or quote_issue(market_quote, now):
            return
        price = d(market_quote["bid" if self.sign == 1 else "ask"])
        bottom, top = sorted((self.stop, self.target))
        if bottom < price < top and bottom < d(market_quote["last"]) < top:
            current = self.decision(price, prospect.closed_at)
            if current and current.kind == prospect.kind:
                return price


async def review_position(client, evidence):
    url, headers, settings = responses_config()
    payload = settings | {
        "instructions": REVIEW_SKILL,
        "input": [{"role": "user", "content": json.dumps(evidence, ensure_ascii=False, allow_nan=False)}],
        "max_output_tokens": 1800,
        "text": {"format": {"type": "json_schema", "name": "perpetual_position_review", "strict": True,
                            "schema": PositionReview.model_json_schema()}},
    }
    async with asyncio.timeout(20):
        response = await client.post(url, headers=headers, json=payload, timeout=20)
    response.raise_for_status()
    result = response.json()
    if result["status"] != "completed":
        raise AgentError("提前退出复核未完成")
    return PositionReview.model_validate_json(response_text(result["output"]))
