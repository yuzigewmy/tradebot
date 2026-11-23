"""General conversation with read-only research tools."""

import asyncio
from datetime import datetime
import json
import logging
from typing import Literal
from zoneinfo import ZoneInfo

import aiohttp
import ccxt
import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from .agent import AgentError, model_configured, responses_config
from .contracts import AnalyzeInput, TIMEFRAMES
from .perpetual_market import MarketError, closed_windows, now_ms
from .perpetual_strategy import detect_trend_setup
from .service import ROOT, analyze

log = logging.getLogger(__name__)
PROMPT = ROOT / "prompts" / "wechat-assistant.md"
BEIJING = ZoneInfo("Asia/Shanghai")
MINUTE = TIMEFRAMES["1m"]
SIDES = {"long": "做多", "short": "做空"}
STRATEGIES = {"pullback": "旧版趋势回踩", "breakout": "旧版收缩放量突破",
              "trend_pullback": "十五分钟趋势回踩", "channel_breakout": "十五分钟区间突破",
              "eth_compression_short": "30 分钟波动收缩做空", "btc_trend_reclaim": "BTC 多周期均线确认"}


def time_label(ts):
    return datetime.fromtimestamp(ts / 1000, BEIJING).isoformat(timespec="seconds")


def signal_message(signal):
    hold = f"\n持仓截止　{time_label(signal['exit_at'])}（北京时间）" if "exit_at" in signal else ""
    agent = f"\n跟踪子 agent　{signal['trade_agent_id']}" if "trade_agent_id" in signal else ""
    position = f"\n模拟逐仓　{signal['margin_budget']} USDT × {signal['leverage']} 倍" if "margin_budget" in signal else ""
    exchange = signal.get("exchange", "OKX")
    if exchange == "Binance":
        mode = {"quotes": "行情", "demo": "Demo", "live": "实盘"}[signal["execution_mode"]]
        position = f"\n计划逐仓　{signal['margin_budget']} USDT × {signal['leverage']} 倍"
        exchange = f"币安 {mode}"
    return (f"【{exchange} 永续开仓信号】\n{signal['instrument']} · {SIDES[signal['side']]}\n"
            f"策略　{STRATEGIES[signal['strategy']]}\n"
            f"开仓参考　{signal['entry']} USDT\n"
            f"止盈　{signal['take_profit']} USDT\n"
            f"止损　{signal['stop_loss']} USDT\n"
            f"费用后收益风险比　{signal['net_reward_risk']} R\n"
            f"入场有效至　{time_label(signal['expires_at'])}（北京时间）{hold}{position}{agent}")


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetArgs(NoArguments):
    asset: Literal["BTC", "ETH"]


class MarketArgs(AssetArgs):
    market: Literal["spot", "perpetual"]


class SignalArgs(NoArguments):
    asset: Literal["BTC", "ETH", "all"]


class BacktestArgs(NoArguments):
    strategy: Literal["all", "channel_breakout", "trend_pullback"]


TOOL_SCHEMAS = {
    "market_snapshot": (MarketArgs, "读取最新已收盘 K 线指标、结构和趋势证据，结果标明交易所。现货有日线、四小时、一小时。永续有一小时、十五分钟、一分钟。"),
    "perpetual_quote": (AssetArgs, "读取当前数据源的 USDT 永续最新成交价、买卖一档、标记价、资金费率及时间。"),
    "recent_signals": (SignalArgs, "查询 BTC、ETH 或两者最近的正式开仓信号、有效状态、开仓参考、止盈和止损。"),
    "monitor_status": (NoArguments, "查询 BTC 与 ETH 永续监控是否运行、最新收盘时间和状态。"),
    "backtest_summary": (BacktestArgs, "读取本地最近一次指定策略的永续模拟报告。查询组合时用 all，单独查询区间突破或趋势回踩时用对应策略。"),
}
TOOLS = [{"type": "function", "name": name, "description": description, "strict": True,
          "parameters": arguments.model_json_schema()} for name, (arguments, description) in TOOL_SCHEMAS.items()]
TOOLS.append({"type": "web_search"})


def format_reply(output):
    for message in reversed(output):
        if message["type"] != "message" or message.get("phase") == "commentary":
            continue
        parts = [part for part in message["content"] if part["type"] in {"output_text", "refusal"}]
        text = "".join(part["text"] if part["type"] == "output_text" else part["refusal"] for part in parts).strip()
        if not text:
            continue
        if not text.startswith("BOSS"):
            text = f"BOSS，{text}"
        urls = dict.fromkeys(annotation["url"] for part in parts for annotation in (part.get("annotations") or ())
                             if annotation["type"] == "url_citation" and len(annotation["url"]) <= 700
                             and annotation["url"].startswith("https://"))
        links = "\n\n来源\n" + "\n".join(list(urls)[:3]) if urls else ""
        available = 3500 - len(links)
        if len(text) > available:
            text = text[:available - 24] + "\n\n（内容较长，可以继续追问。）"
        return text + links
    raise AgentError("对话模型未返回文字")


class WechatResearch:
    def __init__(self, monitors, model_client):
        self.monitors = monitors
        self.model_client = model_client

    def active(self, signal):
        monitor = self.monitors[signal["instrument"]]
        return monitor.signal_is_active(signal, monitor.now())

    async def answer(self, text, conversation):
        if not text:
            return "BOSS，请发文字消息，我可以接着聊。"
        if len(text) > 8000:
            return "BOSS，这条消息超过八千字，请分段发送。"
        if not model_configured():
            return "BOSS，模型尚未配置，对话暂不可用。"
        try:
            async with asyncio.timeout(95):
                reply = await self.run_chat(text, conversation)
        except (AgentError, httpx.HTTPError, TimeoutError, json.JSONDecodeError) as error:
            log.warning("微信对话未完成（%s）", type(error).__name__)
            return "BOSS，本次模型对话未完成，请稍后再试。"
        conversation.append((text, reply))
        return reply

    async def run_chat(self, text, conversation):
        inputs = []
        for question, answer in conversation:
            inputs.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
        inputs.append({"role": "user", "content": text})
        url, headers, settings = responses_config()
        payload = settings | {
            "instructions": PROMPT.read_text(encoding="utf-8") + f"\n本机时间（北京时间）{time_label(now_ms())}。",
            "tools": TOOLS, "tool_choice": "auto", "include": ["reasoning.encrypted_content"], "max_output_tokens": 3800,
        }
        budget = 8
        for round_number in range(5):
            choice = "auto" if budget and round_number < 4 else "none"
            response = await self.model_client.post(url, headers=headers, json=payload | {"input": inputs, "tool_choice": choice}, timeout=45)
            response.raise_for_status()
            result = response.json()
            if result["status"] != "completed":
                raise AgentError("对话模型未完成回复")
            output = result["output"]
            calls = [item for item in output if item["type"] == "function_call"]
            if not calls:
                return format_reply(output)
            if choice == "none":
                break
            inputs.extend(output)
            results = await asyncio.gather(*(self.execute(call) for call in calls[:budget]))
            for index, call in enumerate(calls):
                outcome = results[index] if index < len(results) else {"error": "本轮工具调用已达上限"}
                inputs.append({"type": "function_call_output", "call_id": call["call_id"],
                               "output": json.dumps(outcome, ensure_ascii=False, allow_nan=False)})
            budget -= len(results)
        raise AgentError("对话工具轮次已达上限")

    async def execute(self, call):
        name = call["name"]
        if name not in TOOL_SCHEMAS:
            return {"error": "没有这个工具"}
        try:
            args = TOOL_SCHEMAS[name][0].model_validate_json(call["arguments"])
        except ValidationError:
            return {"error": "工具参数与支持的币种或市场类型不符"}
        try:
            match name:
                case "market_snapshot":
                    return await self.snapshot(args.asset, args.market)
                case "perpetual_quote":
                    instrument = f"{args.asset}-USDT-SWAP"
                    monitor = self.monitors[instrument]
                    data = await monitor.get_quote()
                    return {"exchange": monitor.exchange, "instrument": instrument, **data,
                            "as_of_beijing": time_label(data["ticker_at"]), "mark_at_beijing": time_label(data["mark_at"]),
                            "funding_at_beijing": time_label(data["funding_at"])}
                case "recent_signals":
                    instruments = self.monitors if args.asset == "all" else (f"{args.asset}-USDT-SWAP",)
                    return {"checked_at_beijing": time_label(now_ms()), "signals": {inst: self.recent(inst) for inst in instruments}}
                case "monitor_status":
                    return {inst: {"running": m.running, "mode": m.mode, "selected_strategy": m.selected_strategy,
                                   "status": m.status, "note": m.market_note,
                                   "last_close_beijing": time_label(m.evaluation["closed_at"]) if m.evaluation else None}
                            for inst, m in self.monitors.items()}
                case "backtest_summary":
                    return self.backtest(args.strategy)
        except (MarketError, aiohttp.ClientError, ccxt.BaseError, TimeoutError, json.JSONDecodeError) as error:
            log.warning("微信工具 %s 未取得数据（%s）", name, type(error).__name__)
            return {"error": "本次未取得该工具的数据"}

    def recent(self, instrument):
        fields = ("id", "side", "strategy", "entry", "stop_loss", "take_profit", "net_reward_risk")
        signals = self.monitors[instrument].journal.for_instrument(instrument)[:3]
        return [{**{field: signal[field] for field in fields}, "active": self.active(signal),
                 "strategy_name": STRATEGIES[signal["strategy"]], "direction": SIDES[signal["side"]],
                 "published_at_beijing": time_label(signal["published_at"]), "expires_at_beijing": time_label(signal["expires_at"]),
                 "exit_at_beijing": time_label(signal["exit_at"]) if "exit_at" in signal else None}
                for signal in signals]

    def backtest(self, strategy):
        paths = sorted((ROOT / "artifacts").glob("perpetual-backtest-*/summary.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        fields = ("start_utc", "end_utc_exclusive", "initial_equity", "ending_equity", "net_pnl", "return_pct", "closed_trades",
                  "win_rate_pct", "profit_factor", "max_drawdown_pct", "total_fees", "net_funding", "leverage", "risk_fraction",
                  "fee_rate", "exit_slippage", "exit_counts", "groups")
        for path in paths:
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("selected_strategy", "all") == strategy:
                return {"kind": "历史模拟", "exchange": "OKX", "instruments": list(report["rules"]),
                        "policy_version": report.get("policy_version", "perpetual-entry/v1"), "selected_strategy": strategy,
                        **{key: report[key] for key in fields}}
        return {"error": "本机没有该策略的回测报告"}

    async def snapshot(self, asset, market):
        if market == "spot":
            report = await asyncio.to_thread(analyze, AnalyzeInput(symbol=f"{asset}/USDT"))
            fields = ("price", "trend_label", "structure", "indicators", "zones", "cautions")
            return {"exchange": "OKX", "symbol": report["symbol"], "market": "spot", "as_of_beijing": time_label(report["as_of"]),
                    "timeframes": {tf: {**{field: row[field] for field in fields}, "closed_at_beijing": time_label(row["last_closed_at"])}
                                   for tf, row in report["timeframes"].items()}, "consensus": report["consensus"]}

        instrument = f"{asset}-USDT-SWAP"
        monitor = self.monitors[instrument]
        evaluation = monitor.evaluation
        if monitor.status == "online" and evaluation and monitor.now() - evaluation["closed_at"] < 2 * MINUTE:
            snapshot = {key: evaluation[key] for key in ("closed_at", "context", "setup")}
        else:
            timeframes = ("1m", "15m", "1h")
            data = await asyncio.gather(*(monitor.history(tf) for tf in timeframes))
            bars = {tf: series[0] for tf, series in zip(timeframes, data, strict=True)}
            end = bars["1m"][-1][0] + MINUTE
            windows = closed_windows(bars, end)
            if windows is None or monitor.now() - end >= 2 * MINUTE:
                raise MarketError("各周期最新收盘线未齐全")
            snapshot = {"closed_at": end, **detect_trend_setup(windows)}
        return {"exchange": monitor.exchange, "instrument": instrument, "market": "perpetual", **snapshot,
                "as_of_beijing": time_label(snapshot["closed_at"]),
                "closed_at_beijing_by_timeframe": {tf: time_label(row["closed_at"]) for tf, row in snapshot["context"].items()}}
