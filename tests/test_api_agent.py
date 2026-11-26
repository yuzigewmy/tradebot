import json

from fastapi.testclient import TestClient
import httpx
import pytest

from spot_agent.agent import AgentError, run_agent, validate_answer
from spot_agent.api import app


def test_api_demo_portfolio_export_and_input_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr("spot_agent.service.REPORTS", tmp_path)
    client = TestClient(app)
    response = client.post("/api/analyze", json={"source": "synthetic"})
    assert response.status_code == 200
    report = response.json()
    assert len(report["timeframes"]) == 3
    p = report["timeframes"]["1h"]["price"]
    position = {"quantity": 0, "available_quantity": 0, "cash": 10000, "exit_price": p * 0.95}
    repriced = client.post(f"/api/reports/{report['snapshot_id']}/portfolio", json=position).json()
    assert repriced["as_of"] == report["as_of"]
    assert repriced["portfolio"]["unrealized_pnl"] is None
    exported = client.get(f"/api/reports/{repriced['snapshot_id']}/markdown")
    assert exported.status_code == 200
    assert "合成演示" in exported.text
    assert client.post("/api/analyze", json={"source": "synthetic"}, headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post(f"/api/reports/{report['snapshot_id']}/portfolio", json=position | {"available_quantity": 1}).status_code == 422


def test_model_missing_configuration_and_invented_references_are_visible(tmp_path, monkeypatch):
    monkeypatch.setattr("spot_agent.service.REPORTS", tmp_path)
    monkeypatch.delenv("SPOT_LLM_API_KEY", raising=False)
    client = TestClient(app)
    report = client.post("/api/analyze", json={"source": "synthetic"}).json()
    response = client.post(f"/api/reports/{report['snapshot_id']}/insight", json={})
    assert response.status_code == 409
    assert "未配置" in response.json()["error"]["message"]
    with pytest.raises(AgentError, match="不存在"):
        validate_answer(json.dumps({"answer": "观察结构。", "counter_evidence": ["动量未确认。"], "evidence_ids": ["fake"]}), ["1h:trend"])
    with pytest.raises(AgentError, match="数字"):
        validate_answer(json.dumps({"answer": "上涨80%", "counter_evidence": ["存在反证。"], "evidence_ids": ["1h:trend"]}), ["1h:trend"])


def test_agent_reads_evidence_tools_before_returning_answer(monkeypatch):
    monkeypatch.setenv("SPOT_LLM_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("SPOT_LLM_API_KEY", "test-only")
    monkeypatch.setenv("SPOT_LLM_MODEL", "test-model")
    monkeypatch.setenv("SPOT_LLM_SERVICE_TIER", "ultrafast")
    requests = []
    def model_response(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/v1/responses"
        assert payload["reasoning"] == {"effort": "high"}
        assert payload["store"] is False
        assert payload["stream"] is True
        assert payload["service_tier"] == "ultrafast"
        assert json.loads(payload["input"][0]["content"])["market"]["source"] == "synthetic"
        if len(requests) == 1:
            output = [{"id": "fc_one", "call_id": "call_one", "type": "function_call", "name": "get_evidence", "arguments": json.dumps({"ids": ["1h:trend"]})}]
        else:
            assert payload["input"][-1]["type"] == "function_call_output"
            assert payload["input"][-1]["call_id"] == "call_one"
            answer = {"answer": "趋势证据需要结构配合，等待确认。", "counter_evidence": ["均线有滞后性。"], "evidence_ids": ["1h:trend"]}
            output = [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}]
        event = {"type": "response.completed", "response": {"output": output, "status": "completed", "model": "test-model", "service_tier": "ultrafast"}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=f"event: response.completed\ndata: {json.dumps(event)}\n\n")
    client = httpx.Client(transport=httpx.MockTransport(model_response))
    monkeypatch.setattr("spot_agent.agent.httpx.Client", lambda: client)
    report = {"evidence": [{"id": "1h:trend", "text": "computed"}], "snapshot_id": "snapshot", "source": "synthetic", "exchange": "synthetic", "symbol": "BTC/USDT", "as_of": 1794960000000}
    result = run_agent(report, "解释趋势")
    assert result["tool_calls"] == 1
    assert result["evidence_ids"] == ["1h:trend"]
    assert result["service_tier"] == "ultrafast"
