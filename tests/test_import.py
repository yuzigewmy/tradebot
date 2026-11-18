from fastapi.testclient import TestClient

from spot_agent.api import app


def test_csv_import_is_single_timeframe_playback_and_rejects_bad_prices(tmp_path, monkeypatch):
    monkeypatch.setattr("spot_agent.service.REPORTS", tmp_path)
    client = TestClient(app)
    lines = ["timestamp,open,high,low,close,volume"] + [f"{1700002800000+i*3600000},100,102,99,101,5" for i in range(40)]
    body = {"csv": "\n".join(lines), "timeframe": "1h"}
    response = client.post("/api/import", json=body)
    assert response.status_code == 200
    report = response.json()
    assert report["source"] == "import"
    assert list(report["timeframes"]) == ["1h"]
    assert report["consensus"]["eligible_to_plan_addition"] is False
    bad = body | {"csv": body["csv"].replace("100,102,99,101", "100,98,99,101")}
    assert client.post("/api/import", json=bad).status_code == 422
    assert client.post("/api/import", json={"csv": ""}).status_code == 422
    assert client.post("/api/import", json={"csv": "\n"}).status_code == 422
