import asyncio
from collections import deque
import json
from types import SimpleNamespace

import httpx
import pytest

from spot_agent.perpetual_market import now_ms
from spot_agent.wechat_bridge import BindingStore, WechatBridge
from spot_agent.wechat_ilink import BASE_URL, ILink, ILinkError
from spot_agent.wechat_research import WechatResearch, signal_message


def test_owner_qr_reply_and_live_signal_use_same_bound_context(tmp_path):
    sent, questions = [], []

    def message(seq, owner, context, group=None):
        return {"seq": seq, "from_user_id": owner, "context_token": context, "group_id": group,
                "message_type": 1, "item_list": [{"type": 1, "text_item": {"text": "分析 BTC 永续"}}]}

    async def reply(text, conversation):
        questions.append(text)
        return "已收盘行情回复"

    async def scenario():
        store = BindingStore(tmp_path / "private" / "binding.json")
        ready, push = asyncio.Event(), asyncio.Event()
        first_poll = True

        async def wire(request):
            nonlocal first_poll
            path = request.url.path
            if path.endswith("/get_bot_qrcode"):
                assert request.method == "POST" and request.url.params["bot_type"] == "3"
                return httpx.Response(200, json={"qrcode": "ticket", "qrcode_img_content": f"{BASE_URL}/qr"})
            if path.endswith("/get_qrcode_status"):
                assert request.url.params["qrcode"] == "ticket"
                return httpx.Response(200, json={"status": "confirmed", "ilink_user_id": "owner", "ilink_bot_id": "bot",
                                                 "bot_token": "wx-secret", "baseurl": BASE_URL})
            assert request.headers["Authorization"] == "Bearer wx-secret"
            body = json.loads(request.content)
            if path.endswith("/getupdates"):
                if first_poll:
                    first_poll = False
                    owner = message(3, "owner", "owner-context")
                    return httpx.Response(200, json={"get_updates_buf": "cursor", "msgs": [
                        message(1, "stranger", "other"), message(2, "owner", "group", "group-id"), owner, owner]})
                assert body["get_updates_buf"] == "cursor"
                ready.set()
                await asyncio.Future()
            assert path.endswith("/sendmessage")
            sent.append(body["msg"])
            if body["msg"]["client_id"].startswith("spot-signal-"):
                push.set()
            return httpx.Response(200, json={"ret": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as session:
            bridge = WechatBridge(ILink(session), store, reply, signal_message, lambda signal: signal["expires_at"] > now_ms())
            try:
                assert (await bridge.begin())["qr_available"] and bridge.qr.startswith(b"\x89PNG")
                await asyncio.wait_for(ready.wait(), 4)
                assert questions == ["分析 BTC 永续"] and len(sent) == 1
                assert bridge.view()["has_context"] and "wx-secret" not in str(bridge.view())
                assert store.load().context == "owner-context"
                signal = {"id": "btc-new", "instrument": "BTC-USDT-SWAP", "side": "long", "strategy": "pullback",
                          "entry": 100000.1, "stop_loss": 99981.1, "take_profit": 100101.3, "net_reward_risk": 2,
                          "expires_at": now_ms() + 20_000}
                bridge.publish(signal | {"id": "btc-expired", "expires_at": now_ms() - 1})
                bridge.publish(signal)
                await asyncio.wait_for(push.wait(), 4)
                assert len(sent) == 2
                alert = sent[-1]
                assert alert["to_user_id"] == "owner" and alert["context_token"] == "owner-context"
                text = alert["item_list"][0]["text_item"]["text"]
                assert all(x in text for x in ("做多", "100000.1", "100101.3", "99981.1", "有效至"))
            finally:
                await bridge.unlink()
            assert store.load() is None

    asyncio.run(scenario())


def test_binance_signal_reaches_owner_after_trade_finishes_and_service_restarts(tmp_path):
    from spot_agent.perpetual_trade import TradeNotice
    from spot_agent.wechat_bridge import Binding

    signal = {"id": "btc-signal", "instrument": "BTC-USDT-SWAP", "side": "long", "strategy": "btc_trend_reclaim",
              "entry": 100000.1, "stop_loss": 99000.0, "take_profit": 104000.0, "net_reward_risk": 4,
              "expires_at": now_ms() - 1000, "execution_mode": "live", "exchange": "Binance",
              "trade_agent_id": "trade-example", "margin_percent": 25, "leverage": 30}
    sent = []

    async def scenario():
        store = BindingStore(tmp_path / "private" / "binding.json")
        store.save(Binding(base=BASE_URL, owner="owner", token="mock-key", context="owner-context"))
        finished = asyncio.Event()

        async def wire(request):
            if request.url.path.endswith("/getupdates"):
                await asyncio.Future()
            message = json.loads(request.content)["msg"]
            assert message["to_user_id"] == "owner"
            sent.append(message["item_list"][0]["text_item"]["text"])
            if len(sent) == 2:
                finished.set()
            return httpx.Response(200, json={"ret": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as session:
            bridge = WechatBridge(ILink(session), store, None, signal_message, lambda _: False)
            bridge.publish(signal)
            bridge.publish_trade(TradeNotice("trade-example", "closed", signal["instrument"], "失败", "限价未成交。"))
            restored = WechatBridge(ILink(session), store, None, signal_message, lambda _: False)
            restored.start()
            try:
                await asyncio.wait_for(finished.wait(), 2)
            finally:
                await restored.close()

    asyncio.run(scenario())
    assert sent[0].splitlines()[:2] == ["trade-example", "信号已发布"]
    assert all(term in sent[0] for term in ("币安 实盘", "做多", "100000.1", "99000.0", "104000.0"))
    assert sent[1] == "trade-example\n失败\n限价未成交。"


def test_wechat_rejects_untrusted_host_and_detects_expired_token():
    calls = []

    async def scenario():
        def wire(request):
            calls.append(request.url.host)
            return httpx.Response(200, json={"ret": -1, "errcode": -14})

        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as session:
            ilink = ILink(session)
            with pytest.raises(ILinkError):
                await ilink.updates("https://weixin.qq.com.example.org", "credential", "", 10)
            with pytest.raises(ILinkError) as error:
                await ilink.updates(BASE_URL, "credential", "", 10)
            assert error.value.expired
            assert calls == ["ilinkai.weixin.qq.com"]

    asyncio.run(scenario())


def test_chat_answers_freely_and_uses_read_only_tool_for_followup(monkeypatch):
    monkeypatch.setenv("SPOT_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("SPOT_LLM_API_KEY", "test-only")
    monkeypatch.setenv("SPOT_LLM_MODEL", "test-model")
    conversation = deque(maxlen=8)

    def stream(output):
        event = {"type": "response.completed", "response": {"status": "completed", "output": output}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text=f"event: response.completed\ndata: {json.dumps(event)}\n\n")

    def wire(request):
        data = json.loads(request.content)
        if data.get("stream") is not True:
            return httpx.Response(400, json={"error": {"code": "stream_required"}})
        inputs = data["input"]
        assert data["tool_choice"] == "auto" and data["store"] is False
        if len(inputs) == 1:
            assert inputs[0] == {"role": "user", "content": "介绍一下你自己"}
            text = "BOSS，我可以帮你处理日常问题和加密研究。"
        elif inputs[-1].get("type") == "function_call_output":
            assert json.loads(inputs[-1]["output"])["BTC-USDT-SWAP"]["running"] is True
            assert inputs[-1]["call_id"] == "read_status"
            text = "BOSS，BTC 永续监控正在运行。"
        else:
            assert inputs[1] == {"role": "assistant", "content": conversation[0][1]}
            return stream([{"type": "function_call", "name": "monitor_status", "call_id": "read_status", "arguments": "{}"}])
        return stream([{"type": "message", "content": [{"type": "output_text", "text": text}]}])

    async def scenario():
        monitor = SimpleNamespace(running=True, mode="observe", selected_strategy="all", status="online", market_note="行情在线", evaluation=None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as model:
            assistant = WechatResearch({"BTC-USDT-SWAP": monitor}, model)
            assert "日常问题" in await assistant.answer("介绍一下你自己", conversation)
            assert "正在运行" in await assistant.answer("刚说的那个监控开着吗？", conversation)

    asyncio.run(scenario())


def test_wechat_monitor_status_reports_live_auto_trading_and_s10_details():
    monitor = SimpleNamespace(
        exchange="Binance", settings=SimpleNamespace(mode="live"),
        running=True, mode="signals", selected_strategy="dual_trend_engine",
        status="online", market_note="策略已计算最新收盘 K 线", evaluation={"closed_at": 1795744800000},
    )
    assistant = WechatResearch({"BTC-USDT-SWAP": monitor}, None)
    result = asyncio.run(assistant.execute({"name": "monitor_status", "arguments": "{}"}))
    btc = result["BTC-USDT-SWAP"]
    assert btc["exchange"] == "Binance"
    assert btc["execution_mode"] == "live"
    assert btc["auto_trading"] is True
    assert btc["strategy_name"] == "S10 双引擎顺势"
    assert "S4" in btc["strategy_detail"] and "S3" in btc["strategy_detail"]
    assert "3%" in btc["sizing"] and "10 倍" in btc["sizing"]


def test_chat_does_not_publish_or_remember_partial_reply(monkeypatch):
    monkeypatch.setenv("SPOT_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("SPOT_LLM_API_KEY", "test-only")
    monkeypatch.setenv("SPOT_LLM_MODEL", "test-model")
    conversation = deque(maxlen=8)

    def wire(request):
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text='data: {"type":"response.output_text.delta","delta":"半条回复"}\n\n')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as model:
            assistant = WechatResearch({}, model)
            assert "本次模型对话未完成" in await assistant.answer("测试断流", conversation)
            assert not conversation

    asyncio.run(scenario())
