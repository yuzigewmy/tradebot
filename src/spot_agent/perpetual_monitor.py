"""Live OKX subscription and one independent review worker per instrument."""

import asyncio
from contextlib import asynccontextmanager
import json
from collections import deque
import logging

import aiohttp
import httpx
from pydantic import ValidationError

from .agent import AgentError, model_configured
from .contracts import TIMEFRAMES
from .perpetual_market import CHANNELS, HISTORY_LIMIT, OKX_CANDLES_WS, MarketError, closed_windows, history, instrument_tick, now_ms, parse_candle, quote
from .perpetual_review import refresh_candidate, review_event, review_setup, review_timeout
from .perpetual_strategy import PROFILES, detect_setup, detect_trend_setup, exits_touched, priced_candidate, signal_cooldown

MINUTE = TIMEFRAMES["1m"]
logger = logging.getLogger(__name__)


class SignalJournal:
    def __init__(self, path, on_signal=None):
        self.path = path
        self.on_signal = on_signal
        self.records = deque(maxlen=120)
        if path.exists():
            with path.open(encoding="utf-8") as entries:
                self.records.extend(map(json.loads, entries))

    def append(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        self.records.append(record)
        if self.on_signal is not None:
            self.on_signal(record)

    def for_instrument(self, instrument):
        return [record for record in reversed(self.records) if record["instrument"] == instrument]


class PerpetualMonitor:
    def __init__(self, instrument, market_client, model_client, journal, paper=None, *, profile="v2"):
        self.instrument = instrument
        self.market_client = market_client
        self.model_client = model_client
        self.journal = journal
        self.paper = paper
        self.profile = profile
        self.mode = "observe"
        self.task = None
        self.status = "stopped"
        self.market_note = "监控未启动"
        self.bars = {}
        self.forming = {}
        self.evaluation = None
        self.last_quote = None
        self.latest_job = None
        self.review_state = None
        self.last_evaluated = 0
        self.skip_through = 0
        self.review_requested = asyncio.Event()

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def start(self, mode):
        if mode != self.mode:
            self.invalidate_review("监控方式已切换")
        self.mode = mode
        if not self.running:
            self.status, self.market_note = "connecting", "连接 OKX 并加载 K 线"
            self.task = asyncio.create_task(self.run())
            self.task.add_done_callback(self.on_exit)

    def on_exit(self, task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("永续监控 %s 意外停止", self.instrument, exc_info=task.exception())
            self.status, self.market_note = "error", "监控异常停止；请查看本地服务日志"

    async def stop(self):
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.status, self.market_note = "stopped", "监控已停止"
        self.invalidate_review("监控已停止")

    def invalidate_review(self, note):
        self.latest_job = None
        self.review_requested.clear()
        if self.review_state and self.review_state["status"] in ("queued", "reviewing"):
            self.review_state.update(status="discarded", note=note)

    async def run(self):
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.collect())
            tasks.create_task(self.review_loop())

    async def collect(self):
        while True:
            try:
                await self.stream()
            except (aiohttp.ClientError, MarketError, TimeoutError) as error:
                self.status = "reconnecting"
                self.market_note = f"行情中断，正在重连（{type(error).__name__}）"
                self.invalidate_review("行情已断开，候选作废")
                await asyncio.sleep(3)

    async def stream(self):
        async with self.market_client.ws_connect(OKX_CANDLES_WS, timeout=aiohttp.ClientWSTimeout(ws_close=2)) as ws:
            await ws.send_json({"op": "subscribe", "args": [{"channel": channel, "instId": self.instrument} for channel in CHANNELS]})
            tick, *series = await asyncio.gather(
                instrument_tick(self.market_client, self.instrument),
                *(history(self.market_client, self.instrument, tf) for tf in CHANNELS.values()),
            )
            self.tick = tick
            for timeframe, (rows, current) in zip(CHANNELS.values(), series, strict=True):
                self.bars[timeframe], self.forming[timeframe] = rows, current
            self.status, self.market_note = "online", "OKX 行情已连接"
            self.skip_through = now_ms() // MINUTE * MINUTE
            self.last_evaluated = 0
            self.evaluate()
            while True:
                try:
                    message = await ws.receive(timeout=20)
                except TimeoutError:
                    await ws.send_str("ping")
                    message = await ws.receive(timeout=6)
                if message.type is not aiohttp.WSMsgType.TEXT:
                    raise MarketError("OKX candle WebSocket disconnected")
                if now_ms() - self.bars["1m"][-1][0] > 2 * MINUTE + 20_000:
                    raise MarketError("OKX 一分钟收盘确认超时，需要重同步")
                if message.data == "pong":
                    continue
                packet = json.loads(message.data)
                if "event" in packet:
                    if packet["event"] == "error":
                        raise MarketError(f"OKX 订阅失败（{packet['code']}）")
                    continue
                timeframe = CHANNELS[packet["arg"]["channel"]]
                changed = False
                for raw in packet["data"]:
                    changed = self.ingest(timeframe, *parse_candle(raw)) or changed
                if changed:
                    self.evaluate()

    def ingest(self, timeframe, row, confirmed):
        bars, current = self.bars[timeframe], self.forming[timeframe]
        if row[0] <= bars[-1][0]:
            return False
        if not confirmed:
            if current is None or row[0] >= current[0]:
                self.forming[timeframe] = row
            return False
        if row[0] != bars[-1][0] + TIMEFRAMES[timeframe]:
            raise MarketError(f"{timeframe} 收盘 K 线有缺口，需要重同步")
        if current is not None and current[0] <= row[0]:
            self.forming[timeframe] = None
        bars.append(row)
        del bars[:-HISTORY_LIMIT]
        return True

    def evaluate(self):
        end = self.bars["1m"][-1][0] + MINUTE
        if end <= self.last_evaluated:
            return
        windows = closed_windows(self.bars, end)
        if windows is None:
            self.market_note = "等待各周期收盘确认与连续 K 线"
            return
        initial = self.last_evaluated == 0
        self.last_evaluated = end
        analysis = (detect_trend_setup if self.profile == "v2" else detect_setup)(windows)
        setup = analysis["setup"]
        self.evaluation = {"closed_at": end, **analysis}
        self.market_note = "策略已计算最新收盘 K 线"
        if self.mode == "observe":
            return
        if not setup["strategy"] or initial or end <= self.skip_through or not 0 <= now_ms() - end < MINUTE:
            return
        _, policy_id, _ = PROFILES[self.profile]
        last = next((row["closed_at"] for row in self.journal.for_instrument(self.instrument) if row["policy_id"] == policy_id), None)
        if signal_cooldown(last, end, profile=self.profile):
            self.review_state = {"closed_at": end, "status": "wait", "note": "上一条信号仍在冷却期", "candidate": None, "result": None}
            return
        trigger = "15m" if self.profile == "v2" else "1m"
        self.latest_job = {"instrument": self.instrument, "closed_at": end, "trigger_candle": windows[trigger][-1], **analysis}
        self.review_state = {"closed_at": end, "status": "queued", "note": "策略候选等待报价", "candidate": None, "result": None}
        self.review_requested.set()

    async def review_loop(self):
        while True:
            await self.review_requested.wait()
            self.review_requested.clear()
            job = self.latest_job
            try:
                await self.review(job)
            except (aiohttp.ClientError, httpx.HTTPError, AgentError, MarketError, TimeoutError, ValidationError) as error:
                if self.latest_job is job:
                    reason = str(error) if isinstance(error, (AgentError, MarketError)) else type(error).__name__
                    self.review_state.update(status="error", note=f"本次研判未完成（{reason}）")

    async def review(self, job):
        self.review_state.update(status="reviewing", note="读取盘口、标记价与资金费率")
        end, setup = job["closed_at"], job["setup"]
        market_quote = (await quote(self.market_client, self.instrument, settled=self.profile == "v2") if self.paper is None else
                        await self.paper.market_quote(self.instrument, end))
        if self.latest_job is not job:
            return
        self.last_quote = market_quote
        candidate, reason = priced_candidate(setup, market_quote, self.tick, end, now_ms(), profile=self.profile)
        self.review_state["candidate"] = candidate
        if candidate is None:
            self.review_state.update(status="wait", note=reason)
            return
        if not model_configured():
            self.review_state.update(status="unconfigured", note="模型未配置，规则已计算，不发开仓信号")
            return
        timeout = review_timeout(candidate, now_ms())
        if timeout <= 0:
            self.review_state.update(status="discarded", note="开仓窗口已过期")
            return
        event = review_event(job, candidate, market_quote, reason)
        self.review_state["note"] = "模型正按策略 skill 核对证据"
        result = await review_setup(self.model_client, event, timeout, profile=self.profile)
        if self.latest_job is not job:
            return
        self.review_state["result"] = result
        if result["decision"] != "confirm":
            self.review_state.update(status="wait", note=reason or "模型复核：等待")
            return
        refreshed = (await quote(self.market_client, self.instrument, settled=self.profile == "v2") if self.paper is None else
                     await self.paper.market_quote(self.instrument))
        if self.latest_job is not job:
            return
        self.last_quote = refreshed
        candidate, issue = refresh_candidate(candidate, setup, refreshed, self.tick, self.forming["1m"], now_ms(), profile=self.profile)
        if issue:
            self.review_state.update(status="discarded", note=issue)
            return
        signal = {
            **candidate, "instrument": self.instrument, "published_at": now_ms(),
            "id": f"{self.instrument}:{end}:{candidate['policy_id']}", "event": event, "trigger_candle": job["trigger_candle"], "verified_quote": refreshed,
            "model_review": result,
        }
        self.review_state["candidate"] = candidate
        self.journal.append(signal)
        if self.paper is not None:
            self.paper.enqueue(signal)
        self.review_state.update(status="signal", note="候选通过模型与报价复核")

    def signal_is_active(self, signal, now):
        return (self.mode == "signals" and self.status == "online" and self.skip_through < signal["closed_at"] and now < signal["expires_at"]
                and signal["policy_id"] == PROFILES[self.profile][1]
                and not exits_touched(signal, self.forming["1m"]))

    def view(self):
        signals = self.journal.for_instrument(self.instrument)[:20]
        now, active_id = now_ms(), None
        if signals and self.signal_is_active(signals[0], now):
            active_id = signals[0]["id"]
        policy, policy_id, _ = PROFILES[self.profile]
        return {
            "instrument": self.instrument, "mode": self.mode, "running": self.running,
            "status": self.status, "market_note": self.market_note, "server_time": now,
            "model_configured": model_configured(), "policy_id": policy_id, "policy_version": policy["version"],
            "charts": {tf: {"closed": bars[-100:], "current": self.forming[tf]} for tf, bars in self.bars.items()},
            "evaluation": self.evaluation, "quote": self.last_quote, "review": self.review_state,
            "signals": signals, "active_signal_id": active_id,
        }


@asynccontextmanager
async def live_monitors(journal_path, instruments, on_signal=None):
    journal = SignalJournal(journal_path, on_signal)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=True) as market_client, httpx.AsyncClient() as model_client:
        monitors = {key: PerpetualMonitor(key, market_client, model_client, journal) for key in instruments}
        try:
            yield monitors
        finally:
            await asyncio.gather(*(monitor.stop() for monitor in monitors.values()))
