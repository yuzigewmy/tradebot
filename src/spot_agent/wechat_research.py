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

from .agent import AgentError, astream_response, model_configured, responses_config
from .contracts import AnalyzeInput, TIMEFRAMES
from .perpetual_dedicated import DEDICATED_PROFILES
from .perpetual_dual import DUAL_LEVERAGE, DUAL_MARGIN_PERCENT, DUAL_MAX_STOP_PCT
from .perpetual_macro import MACRO_PROFILES
from .perpetual_market import MarketError, closed_windows, now_ms
from .perpetual_router import AUTO_REGIME_STRATEGY
from .perpetual_strategy import detect_trend_setup
from .service import ROOT, analyze

log = logging.getLogger(__name__)
PROMPT = ROOT / "prompts" / "wechat-assistant.md"
BEIJING = ZoneInfo("Asia/Shanghai")
MINUTE = TIMEFRAMES["1m"]
SIDES = {"long": "做多", "short": "做空"}
STRATEGIES = {"pullback": "旧版趋势回踩", "breakout": "旧版收缩放量突破",
              "trend_pullback": "十五分钟趋势回踩", "channel_breakout": "十五分钟区间突破",
              "eth_compression_short": "30 分钟波动收缩做空", "btc_trend_reclaim": "BTC 多周期均线确认",
              "dual_trend_engine": "S10 双引擎顺势",
              "macro_a1_aggressive": "A1 宏观双引擎 · 激进版",
              "macro_a1_steady": "A1 宏观双引擎 · 稳健版",
              "auto_regime_router": "自动切换 A（强单边 S10 / 震荡 A1）",
              **{key: profile.label for key, profile in DEDICATED_PROFILES.items()}}
ENGINES = {"s4_pullback": "S4 顺势回踩", "s3_keltner": "S3 通道突破",
           "m3_4h_supertrend": "M3 4H超趋势", "m5_1d_donchian": "M5 1D唐奇安CTA",
           "m7_4h_squeeze": "4H TTM压缩突破", "m2_1d_donchian20": "1D 唐奇安20 慢速海龟",
           "m7_4h_st14_38": "4H 宽超趋势 ST(14,3.8)", "m4_1d_st30": "1D 宽超趋势 ST(10,3.0)"}


def time_label(ts):
    return datetime.fromtimestamp(ts / 1000, BEIJING).isoformat(timespec="seconds")


def signal_message(signal):
    hold = f"\n持仓截止　{time_label(signal['exit_at'])}（北京时间）" if "exit_at" in signal else ""
    agent = f"\n跟踪子 agent　{signal['trade_agent_id']}" if "trade_agent_id" in signal else ""
    position = f"\n模拟逐仓　{signal['margin_budget']} USDT × {signal['leverage']} 倍" if "margin_budget" in signal else ""
    exchange = signal.get("exchange", "OKX")
    if exchange == "Binance":
        mode = {"quotes": "行情", "demo": "Demo", "live": "实盘"}[signal["execution_mode"]]
        if "risk_percent" in signal:
            position = f"\n计划逐仓　单笔风险 {signal['risk_percent']:g}% × {signal['leverage']} 倍"
        elif "margin_percent" in signal:
            position = f"\n计划逐仓　钱包余额的 {signal['margin_percent']}% × {signal['leverage']} 倍"
        else:
            position = f"\n计划逐仓　{signal['margin_budget']} USDT × {signal['leverage']} 倍"
        exchange = f"币安 {mode}"
    engine = f"（{ENGINES[signal['engine']]}）" if signal.get("engine") in ENGINES else ""
    return (f"【{exchange} 永续开仓信号】\n{signal['instrument']} · {SIDES[signal['side']]}\n"
            f"策略　{STRATEGIES[signal['strategy']]}{engine}\n"
            f"开仓参考　{signal['entry']} USDT\n"
            f"止盈　{signal['take_profit']} USDT\n"
            f"止损　{signal['stop_loss']} USDT\n"
            f"费用后收益风险比　{signal['net_reward_risk']} R\n"
            f"入场有效至　{time_label(signal['expires_at'])}（北京时间）{hold}{position}{agent}")


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetArgs(NoArguments):
    asset: Literal["BTC", "ETH", "SOL"]


class MarketArgs(AssetArgs):
    market: Literal["spot", "perpetual"]


class SignalArgs(NoArguments):
    asset: Literal["BTC", "ETH", "SOL", "all"]


class BacktestArgs(NoArguments):
    strategy: Literal["all", "channel_breakout", "trend_pullback"]


class RegistrationListArgs(NoArguments):
    status: Literal["pending", "approved", "rejected", "all"] = "pending"


class ReviewRegistrationArgs(NoArguments):
    username: str
    decision: Literal["approve", "reject"]
    note: str = ""


TOOL_SCHEMAS = {
    "market_snapshot": (MarketArgs, "读取最新已收盘 K 线指标、结构和趋势证据，结果标明交易所。现货有日线、四小时、一小时。永续有一小时、十五分钟、一分钟。"),
    "perpetual_quote": (AssetArgs, "读取当前数据源的 USDT 永续最新成交价、买卖一档、标记价、资金费率及时间。"),
    "recent_signals": (SignalArgs, "查询 BTC、ETH、SOL 或全部最近的正式开仓信号、有效状态、开仓参考、止盈和止损。"),
    "monitor_status": (NoArguments, "查询 BTC、ETH、SOL 永续监控是否运行、交易所与执行环境（实盘/Demo/研究）、是否已开启自动下单、当前策略与风控参数及最新收盘时间。"),
    "backtest_summary": (BacktestArgs, "读取本地最近一次指定策略的永续模拟报告。查询组合时用 all，单独查询区间突破或趋势回踩时用对应策略。"),
    "list_registrations": (RegistrationListArgs, "查询加密研究台 Web 端用户注册申请列表（待审核 pending、已通过 approved、已拒绝 rejected 或全部 all）。"),
    "review_registration": (ReviewRegistrationArgs, "审批加密研究台 Web 端注册申请：approve 放行并授予登录权限（不含账户栏），reject 拒绝申请或撤销已通过用户的登录权限。"),
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
    def __init__(self, monitors, model_client, user_store=None):
        self.monitors = monitors
        self.model_client = model_client
        self.user_store = user_store

    async def compose_registration_notice(self, record):
        note_text = record["note"] or "无"
        fallback = (
            f"BOSS，收到一条新的加密研究台注册申请，请审批：\n"
            f"申请账号：{record['username']}\n"
            f"姓名/称呼：{record['display_name']}\n"
            f"申请说明：{note_text}\n"
            f"提交时间：{record['created_at_beijing']}（北京时间）\n"
            f"回复「通过 {record['username']}」放行登录（不开放账户栏），回复「拒绝 {record['username']}」驳回。"
        )
        if not model_configured() or self.model_client is None:
            return fallback
        url, headers, settings = responses_config()
        payload = settings | {
            "instructions": (
                "你是 BOSS 的微信量化助手。现在有一名新用户在加密研究台提交了注册申请，"
                "请用简洁、正式、易读的纯中文微信消息汇报给 BOSS（必须以“BOSS，”开头，不用 Markdown 表格或多余装饰），"
                "必须准确包含申请账号、姓名/称呼、申请说明、北京时间，并明确提示 BOSS 直接回复“通过 <账号>”或“拒绝 <账号>”即可完成审批，"
                "同时注明审核通过后的普通用户登录不展示账户栏。"
            ),
            "input": [{"role": "user", "content": json.dumps(record, ensure_ascii=False)}],
            "max_output_tokens": 500,
        }
        try:
            async with asyncio.timeout(25):
                result = await astream_response(self.model_client, url, headers, payload, 22)
            return format_reply(result["output"])
        except (AgentError, httpx.HTTPError, TimeoutError, json.JSONDecodeError) as error:
            log.warning("注册通知模型生成失败（%s），发送结构化原信息", type(error).__name__)
            return fallback

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
            result = await astream_response(self.model_client, url, headers, payload | {"input": inputs, "tool_choice": choice}, 45)
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
                    status_map = {}
                    for inst, m in self.monitors.items():
                        exec_mode = getattr(getattr(m, "settings", None), "mode", "off")
                        auto_trading = bool(exec_mode in ("demo", "live") and m.running and m.mode == "signals")
                        item = {
                            "exchange": getattr(m, "exchange", "OKX"),
                            "execution_mode": exec_mode,
                            "auto_trading": auto_trading,
                            "running": m.running, "mode": m.mode,
                            "selected_strategy": m.selected_strategy,
                            "strategy_name": STRATEGIES.get(m.selected_strategy, m.selected_strategy),
                            "status": m.status, "note": m.market_note,
                            "last_close_beijing": time_label(m.evaluation["closed_at"]) if m.evaluation else None,
                        }
                        if m.selected_strategy == "dual_trend_engine":
                            item["strategy_detail"] = (
                                f"S10 双引擎顺势：4H 趋势过滤 + 1H S4 顺势回踩(RSI复位) 与 S3 Keltner 通道突破，多空对称，"
                                f"固定钱包余额 {DUAL_MARGIN_PERCENT}% 保证金 × {int(DUAL_LEVERAGE)} 倍逐仓（名义敞口 {DUAL_MARGIN_PERCENT * int(DUAL_LEVERAGE) / 100:g} 倍），"
                                f"止损上限 {DUAL_MAX_STOP_PCT * 100:.1f}%（严守强平安全垫），最长持仓 48 小时并带保本/移动止损"
                            )
                        elif m.selected_strategy in DEDICATED_PROFILES:
                            p = DEDICATED_PROFILES[m.selected_strategy]
                            item["strategy_detail"] = p.detail
                        elif m.selected_strategy in MACRO_PROFILES:
                            p = MACRO_PROFILES[m.selected_strategy]
                            item["strategy_detail"] = (
                                f"{p.label}：4H Supertrend(10,3.0)+1D EMA50 与 1D Donchian(14) CTA 双宏观引擎，多空对称，"
                                f"单笔风险 {p.risk_percent}% × {int(p.leverage)} 倍逐仓（名义上限 {p.max_notional_multiple} 倍，"
                                f"止损上限 {p.max_stop_pct * 100:.1f}%），带 1.5R 保本与移动止损"
                            )
                        elif m.selected_strategy == AUTO_REGIME_STRATEGY:
                            regime = (m.evaluation or {}).get("setup", {}).get("regime")
                            if regime:
                                item["regime"] = regime
                                item["strategy_detail"] = (
                                    f"自动切换 A：当前识别为「{regime['label']}」（1D ADX {regime['d1_adx']} / 4H ADX {regime['h4_adx']} / "
                                    f"1D-4H {'同向' if regime['aligned'] else '分歧'}），强单边(1D ADX≥25 且 4H ADX≥22 且同向)自动走 S10 8%风险×30倍，"
                                    f"震荡盘自动切 A1 激进 8%风险×15倍"
                                )
                            else:
                                item["strategy_detail"] = "自动切换 A：强单边自动走 S10(8%风险×30倍)，震荡盘自动切 A1 激进(8%风险×15倍)，等待收盘计算"
                        if exec_mode in ("demo", "live"):
                            if m.selected_strategy == AUTO_REGIME_STRATEGY:
                                regime = (m.evaluation or {}).get("setup", {}).get("regime")
                                if regime:
                                    item["sizing"] = (
                                        f"当前 {regime['label']} · 单笔止损风险 {regime['active_risk_percent']:g}% × "
                                        f"{regime['active_leverage']} 倍逐仓（名义敞口上限 {regime['active_max_notional']:g} 倍）"
                                    )
                                else:
                                    item["sizing"] = "自动切换：强单边 8%×30倍 / 震荡盘 8%×15倍逐仓"
                            elif m.selected_strategy in DEDICATED_PROFILES:
                                p = DEDICATED_PROFILES[m.selected_strategy]
                                item["sizing"] = f"单笔止损风险 {p.risk_percent}% × {int(p.leverage)} 倍逐仓（名义敞口上限 {p.max_notional_multiple} 倍）"
                            elif m.selected_strategy in MACRO_PROFILES:
                                p = MACRO_PROFILES[m.selected_strategy]
                                item["sizing"] = f"单笔止损风险 {p.risk_percent}% × {int(p.leverage)} 倍逐仓（名义敞口上限 {p.max_notional_multiple} 倍）"
                            else:
                                item["sizing"] = f"钱包余额 {DUAL_MARGIN_PERCENT}% × {int(DUAL_LEVERAGE)} 倍逐仓"
                        status_map[inst] = item
                    return status_map
                case "backtest_summary":
                    return self.backtest(args.strategy)
                case "list_registrations":
                    if self.user_store is None:
                        return {"error": "注册用户存储未初始化"}
                    return {"status_filter": args.status, "users": self.user_store.list_users(args.status)}
                case "review_registration":
                    if self.user_store is None:
                        return {"error": "注册用户存储未初始化"}
                    try:
                        updated = self.user_store.review(args.username, args.decision, args.note)
                    except KeyError as exc:
                        return {"error": str(exc).strip("'")}
                    return {"ok": True, "user": updated, "account_tab_visible": False}
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
