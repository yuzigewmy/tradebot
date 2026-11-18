from copy import deepcopy

from spot_agent.contracts import AnalyzeInput
from spot_agent.service import analyze
from spot_agent.strategy import build_strategy


def test_old_cross_timeframe_zones_do_not_produce_negative_or_infinite_ratio(tmp_path, monkeypatch):
    monkeypatch.setattr("spot_agent.service.REPORTS", tmp_path)
    frames = deepcopy(analyze(AnalyzeInput(source="synthetic"))["timeframes"])
    frames["4h"]["zones"] = {"support": {"low": 100, "high": 105}, "resistance": {"low": 110, "high": 115}}
    for price in [100, 120]:
        frames["1h"]["price"] = price
        result = build_strategy(frames)
        assert result["gross_zone_reward_risk"] is None
