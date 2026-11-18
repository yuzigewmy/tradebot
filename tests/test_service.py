from spot_agent.contracts import AnalyzeInput
from spot_agent.service import analyze, load_report


def test_full_demo_report_reloads_same_snapshot_and_uses_one_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr("spot_agent.service.REPORTS", tmp_path)
    report = analyze(AnalyzeInput(source="synthetic"))
    assert load_report(report["snapshot_id"]) == report
    assert report["source"] == "synthetic"
    assert len(report["evidence"]) == 18
    assert len({a["as_of"] for a in report["timeframes"].values()}) == 1
    assert "合成演示" in report["report"]
    assert all(a["last_closed_at"] <= report["as_of"] for a in report["timeframes"].values())
