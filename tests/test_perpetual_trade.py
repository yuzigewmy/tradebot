import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from spot_agent.perpetual_history import MINUTE
from spot_agent.perpetual_market import now_ms
from spot_agent.perpetual_monitor import SignalJournal, live_monitors
from spot_agent.perpetual_outcomes import SignalOutcomes
from spot_agent.perpetual_trade import TradeAgent, TradeNotice, closed_notice
from spot_agent.wechat_bridge import Binding, BindingStore, WechatBridge
from spot_agent.wechat_ilink import BASE_URL, ILink

T0 = 1795392000000


def record(**changes):
    return {"id": "BTC-USDT-SWAP:0:v2", "instrument": "BTC-USDT-SWAP", "side": "long",
            "closed_at": T0, "published_at": T0 + 10_100, "expires_at": T0 + MINUTE,
            "entry": 100, "stop_loss": 95, "take_profit": 115, "exit_at": T0 + 20 * MINUTE,
            "strategy": "channel_breakout", "event": {"setup": {"stop_anchor": 98.0}}} | changes


def test_live_service_assigns_a_stable_distinct_child_to_each_simulated_entry(tmp_path, monkeypatch):
    path = tmp_path / "signals.jsonl"
    started = asyncio.Queue()

    async def run(worker):
        started.put_nowait((worker.signal["id"], asyncio.current_task().get_name()))
        await asyncio.Future()

    monkeypatch.setattr(TradeAgent, "run", run)

    async def children():
        async with live_monitors(path, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"], on_trade=lambda notice: None) as monitors:
            journal = monitors["BTC-USDT-SWAP"].journal
            if not journal.records:
                journal.append(record())
                journal.append(record(id="ETH-USDT-SWAP:0:v2", instrument="ETH-USDT-SWAP", side="short"))
                journal.append(record(id="expired", published_at=T0 + MINUTE - 1))
            assert not monitors["BTC-USDT-SWAP"].running
            return dict([await asyncio.wait_for(started.get(), 2) for _ in range(2)])

    async def scenario():
        before = await children()
        after = await children()
        persisted = SignalJournal(path)
        assert before == after == {r["id"]: r["trade_agent_id"] for r in persisted.records if "trade_agent_id" in r}
        assert len(set(before.values())) == 2 and "expired" not in before

    asyncio.run(scenario())


@pytest.mark.parametrize("side,closes,expected", [
    ("long", [(106, 106), (104, 104), (103, 103)], "early_take_profit"),
    ("short", [(94, 94), (96, 96), (97, 97)], "early_take_profit"),
    ("long", [(97, 97), (97, 97)], "early_stop_loss"),
    ("short", [(103, 103), (103, 103)], "early_stop_loss"),
])
def test_trade_agent_uses_post_entry_closed_structure(side, closes, expected):
    overrides = ({"stop_loss": 105, "take_profit": 85, "event": {"setup": {"stop_anchor": 102.0}}}
                 if side == "short" else {})
    signal = record(side=side, trade_agent_id="trade-test", **overrides)
    agent = TradeAgent(signal, None, None, None, None)
    rows = [[T0, 99, 114, 86, 100, 10]]  # Unordered pre-entry range must not create a profit peak.
    rows.extend([T0 + i * MINUTE, c, max(h, c), min(h, c), c, 10] for i, (c, h) in enumerate(closes, 1))
    prospect = agent.observe(rows)
    assert prospect.kind == expected
    assert prospect.closed_at == rows[-1][0] + MINUTE


@pytest.mark.parametrize("status,price,verdict", [
    ("take_profit", 115.0, "成功"), ("stop_loss", 95.0, "失败"),
    ("time_limit", 100.05, "失败"), ("time_limit", 101.0, "成功"),
])
def test_closing_notice_uses_the_same_id_and_exact_three_line_format(status, price, verdict):
    notice = closed_notice(record(trade_agent_id="trade-test"), {"status": status, "at": T0 + 2 * MINUTE, "price": price})
    agent_id, result, body = notice.text.splitlines()
    assert (agent_id, result) == ("trade-test", verdict)
    assert str(price) in body and "BTC-USDT-SWAP" in body


def test_trade_delivery_survives_restart_and_ignores_stale_early_notice(tmp_path):
    received = []
    final = TradeNotice("trade-test", "closed", "BTC-USDT-SWAP", "失败", "模拟止损结案")

    async def scenario():
        store = BindingStore(tmp_path / "wechat" / "binding.json")
        store.save(Binding(base=BASE_URL, owner="owner", token="mock-key", context="owner-context"))
        sent = asyncio.Event()

        async def wire(request):
            if request.url.path.endswith("/getupdates"):
                await asyncio.Future()
            message = json.loads(request.content)["msg"]
            received.append(message)
            if len(received) == 1:
                restored.trade_ready.set()
                return httpx.Response(503)
            sent.set()
            return httpx.Response(200, json={"ret": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as session:
            bridge = WechatBridge(ILink(session), store, None, None, None)
            bridge.publish_trade(replace(final, kind="early_stop_loss", body="过时提前止损", expires_at=now_ms() + MINUTE))
            bridge.publish_trade(final)
            restored = WechatBridge(ILink(session), store, None, None, None)
            restored.start()
            try:
                await asyncio.wait_for(sent.wait(), 2)
                assert restored.trade_outbox[final.key]["delivered"] is True
            finally:
                await restored.close()
            final_restart = WechatBridge(ILink(session), store, None, None, None)
            final_restart.publish_trade(final)
            assert final_restart.trade_outbox[final.key]["delivered"] is True
        first_attempt, alert = received
        assert first_attempt == alert
        assert alert["to_user_id"] == "owner" and alert["context_token"] == "owner-context"
        assert alert["item_list"][0]["text_item"]["text"] == "trade-test\n失败\n模拟止损结案"

    asyncio.run(scenario())


async def trade_lifecycle(tmp_path, monkeypatch, *, market_touches_stop=False):
    now = T0 + 3 * MINUTE + 3000
    signal = record(trade_agent_id="trade-life")
    notices = []
    reviewed = asyncio.Event()
    calls = []

    class WorkflowMarket:
        async def candles(self, endpoint, instrument, step, start, end):
            candles = {
                T0: [T0, 100, 103, 99, 100, 10],
                T0 + MINUTE: [T0 + MINUTE, 100, 100, 97, 97, 10],
                T0 + 2 * MINUTE: [T0 + 2 * MINUTE, 97, 98, 97, 97, 10],
                T0 + 3 * MINUTE: [T0 + 3 * MINUTE, 97, 97, 94, 94, 10],
            }
            if step == MINUTE:
                return [candles[t] for t in range(start, end, MINUTE)]
            return [[at, 97, 97, 94, 94, 1] if at == start + 1000 else [at, 97, 97, 97, 97, 0]
                    for at in range(start, end, step)]

        async def trade_ticks(self, instrument, at):
            return [(at + 500, 1, 94)]

    async def quote(*_):
        return {"bid": 97, "ask": 97.01, "last": 97, "mark": 97,
                "ticker_at": now, "mark_at": now, "funding_at": T0 + 60 * MINUTE, "funding_rate": 0}

    async def latest_candle(*_, **__):
        return [[str(v) for v in (T0 + 3 * MINUTE, 97, 97.01, 94 if market_touches_stop else 97, 97, 10, 10, 970, 0)]]

    def model_response(request):
        payload = json.loads(request.content)
        data = json.loads(payload["input"][0]["content"])
        assert payload["store"] is False and payload["text"]["format"]["name"] == "perpetual_position_review"
        assert data["agent_id"] == "trade-life" and data["proposed_action"] == "early_stop_loss"
        calls.append(data)
        reviewed.set()
        assert data["recent_closed_candles"][-1][4] == 97
        text = json.dumps({"decision": "early_stop_loss"})
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]})

    monkeypatch.setenv("SPOT_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("SPOT_LLM_API_KEY", "test-only")
    monkeypatch.setenv("SPOT_LLM_MODEL", "test-model")
    monkeypatch.setattr("spot_agent.perpetual_trade.now_ms", lambda: now)
    monkeypatch.setattr("spot_agent.perpetual_trade.quote", quote)
    monkeypatch.setattr("spot_agent.perpetual_trade.public_data", latest_candle)

    journal = SignalJournal(tmp_path / "signals.jsonl")
    journal.append(signal)
    settled = SignalOutcomes(WorkflowMarket(), journal)
    async with httpx.AsyncClient(transport=httpx.MockTransport(model_response)) as model:
        worker = TradeAgent(signal, settled, None, model, notices.append)
        rows = await settled.update(signal, T0 + 3 * MINUTE)
        await worker.advise(worker.observe(rows))
        assert len(calls) == 1 and reviewed.is_set()
        now += MINUTE
        await worker.run()
    return notices


@pytest.mark.parametrize("market_touches_stop", [False, True])
def test_trade_child_revalidates_the_model_alert_and_still_sends_the_close(tmp_path, monkeypatch, market_touches_stop):
    notices = asyncio.run(trade_lifecycle(tmp_path, monkeypatch, market_touches_stop=market_touches_stop))
    assert [msg.kind for msg in notices] == (["closed"] if market_touches_stop else ["early_stop_loss", "closed"])
    assert [msg.text.splitlines()[:2] for msg in notices] == [["trade-life", "失败"] for _ in notices]
    assert all(len(msg.text.splitlines()) == 3 for msg in notices)
    assert "触及止损" in notices[-1].body
