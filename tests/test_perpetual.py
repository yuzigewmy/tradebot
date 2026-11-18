import asyncio
from decimal import Decimal
import json
import math

import httpx
import pytest

from spot_agent.contracts import TIMEFRAMES
from spot_agent.perpetual_market import closed_windows, parse_candle
from spot_agent.perpetual_monitor import PerpetualMonitor, SignalJournal
from spot_agent.perpetual_strategy import POLICY_ID, candidate_issue, detect_setup, exits_touched, priced_candidate


def rows(timeframe, end):
    duration = TIMEFRAMES[timeframe]
    return [[end - (160 - i) * duration, 100, 101, 99, 100, 10] for i in range(160)]


def rising_context(end):
    windows = {tf: rows(tf, end) for tf in ("1m", "15m", "1h")}
    for tf in ("15m", "1h"):
        for i, bar in enumerate(windows[tf]):
            price = 85 + i * 0.095
            bar[1:5] = [price, price + 0.5, price - 0.5, price]
    return windows


def test_confirmation_and_aligned_windows_exclude_unclosed_and_later_context():
    end = 1794960000000
    bars = {tf: rows(tf, end) for tf in ("1m", "15m", "1h")}
    live = [str(v) for v in [end, 100, 110, 99, 108, 10, 20, 30, 0]]
    row, closed = parse_candle(live)
    assert not closed and row == [end, 100, 110, 99, 108, 20]
    bars["15m"].pop()
    assert closed_windows(bars, end) is None
    bars["15m"] = rows("15m", end) + [[end, 100, 150, 90, 140, 100]]
    assert closed_windows(bars, end)["15m"][-1][0] == end - TIMEFRAMES["15m"]
    bars["1m"].pop(-3)
    assert closed_windows(bars, end) is None


@pytest.mark.parametrize("direction", ["long", "short"])
def test_prices_geometrically_valid_and_meet_net_reward_after_costs(direction):
    t = 1794960000000
    setup = {"side": direction, "strategy": "pullback", "close": 100, "atr": 1, "stop_anchor": 99 if direction == "long" else 101}
    quote = {"bid": 99.99, "ask": 100.01, "mark": 100, "funding_rate": 0.0005 if direction == "long" else -0.0005, "funding_at": t + 1800000, "ticker_at": t, "mark_at": t}
    plan, reason = priced_candidate(setup, quote, Decimal("0.01"), t, t)
    assert reason is None and plan["funding_cost_rate"] > 0
    assert plan["net_reward_risk"] >= 2
    assert plan["stop_loss"] < plan["entry"] < plan["take_profit"] if direction == "long" else plan["take_profit"] < plan["entry"] < plan["stop_loss"]
    assert priced_candidate(setup, quote | {"ask": 100.2}, Decimal("0.01"), t, t)[0] is None


def test_breakout_uses_previous_range_and_volume_not_trigger():
    end = 1794960000000
    windows = rising_context(end)
    for i, bar in enumerate(windows["1m"][:-1]):
        price = 100 + (1 if i < 137 else 0.08) * math.sin(i)
        bar[1:5] = [price, price + 0.18, price - 0.18, price]
    windows["1m"][-1][1:] = [100, 100.64, 99.96, 100.60, 20]
    analysis = detect_setup(windows)
    assert analysis["setup"]["strategy"] == "breakout"
    assert analysis["setup"]["volume_ratio"] == 2
    assert analysis["setup"]["prior_high"] < 100.60


def test_candidate_revalidation_rejects_drift_and_touch():
    t = 1794960000000
    setup = {"side": "long", "strategy": "pullback", "close": 100, "atr": 1, "stop_anchor": 99}
    quote = {"bid": 99.99, "ask": 100.01, "mark": 100, "last": 100, "funding_rate": 0, "funding_at": t + 3600000, "ticker_at": t, "mark_at": t}
    plan, _ = priced_candidate(setup, quote, Decimal("0.01"), t, t)
    moved = quote | {"bid": 100.02, "ask": 100.04, "mark": 100.03, "last": 100.03}
    assert candidate_issue(plan, setup, moved, t) == "复核后盘口无法按候选价或更优价入场"
    assert exits_touched(plan, [t, 100, plan["take_profit"], 100, 100, 10])
    assert not exits_touched(plan, [t + 60000, 100, plan["take_profit"], 100, 100, 10])


@pytest.mark.parametrize("side,late", [("long", False), ("short", False), ("long", True)])
def test_review_publishes_exact_candidate_or_discards_on_expiry(tmp_path, monkeypatch, side, late):
    end = 1794960000000
    now = end + 1200
    windows = rising_context(end)
    windows["1m"][-2][4] = 99.8
    windows["1m"][-1][4:] = [100.2, 15]
    if side == "short":
        for bars in windows.values():
            for bar in bars:
                o, h, l, c = bar[1:5]
                bar[1:5] = [200 - o, 200 - l, 200 - h, 200 - c]
    market_price = windows["1m"][-1][4]
    model_candidates = []

    async def market_quote(*_):
        return {"bid": market_price - 0.01, "ask": market_price + 0.01, "mark": market_price, "last": market_price, "ticker_at": now, "mark_at": now, "funding_rate": 0, "funding_at": end + 3600000}

    def model_reply(request):
        nonlocal now
        payload = json.loads(request.content)
        event = json.loads(payload["input"][0]["content"])["event"]
        candidate = event["execution"]["candidate"]
        assert "pullback_adx_15m_pass" in payload["instructions"]
        assert candidate["side"] == side and candidate["strategy"] == "pullback"
        assert candidate["policy_id"] == POLICY_ID
        model_candidates.append(candidate)
        if late:
            now = candidate["expires_at"]
        answer = {"decision": "confirm", "analysis": "方向一致。", "counter_evidence": "注意盘口变化。", "evidence_ids": ["setup", "execution"]}
        return httpx.Response(200, json={"status": "completed", "model": "test-model", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}],
        }]})

    monkeypatch.setenv("SPOT_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("SPOT_LLM_API_KEY", "test-only")
    monkeypatch.setenv("SPOT_LLM_MODEL", "test-model")
    monkeypatch.setattr("spot_agent.perpetual_monitor.now_ms", lambda: now)
    monkeypatch.setattr("spot_agent.perpetual_monitor.quote", market_quote)
    journal = SignalJournal(tmp_path / "signals.jsonl")

    async def evaluate_and_review():
        async with httpx.AsyncClient(transport=httpx.MockTransport(model_reply)) as client:
            monitor = PerpetualMonitor("BTC-USDT-SWAP", None, client, journal)
            monitor.tick = Decimal("0.01")
            monitor.bars = windows
            monitor.forming = dict.fromkeys(windows)
            monitor.status = "online"
            monitor.skip_through = monitor.last_evaluated = end - TIMEFRAMES["1m"]
            monitor.evaluate()
            await monitor.review(monitor.latest_job)
            return monitor.view()

    state = asyncio.run(evaluate_and_review())
    if late:
        assert state["signals"] == []
        assert state["review"]["status"] == "discarded"
    else:
        signal, = SignalJournal(journal.path).for_instrument("BTC-USDT-SWAP")
        assert {key: signal[key] for key in model_candidates[0]} == model_candidates[0]
        assert state["active_signal_id"] == signal["id"]
