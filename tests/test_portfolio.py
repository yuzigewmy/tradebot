import pytest

from spot_agent.contracts import PortfolioInput
from spot_agent.portfolio import assess_portfolio


def inputs(**changes):
    values = dict(quantity=1, available_quantity=1, cash=900, exit_price=90, risk_percent=2, max_weight_percent=50, fee_percent=0.1, slippage_percent=0.1)
    return PortfolioInput(**(values | changes))


def test_existing_risk_is_deducted_and_costs_are_included():
    result = assess_portfolio(inputs(), 100, True)
    assert 0 < result["additional_quantity"] < 1
    assert result["existing_risk"] + result["additional_quantity"] * result["risk_per_new_coin"] <= result["risk_budget"] + 1e-8
    assert result["unrealized_pnl"] is None


def test_addition_satisfies_post_fee_weight_and_cash():
    result = assess_portfolio(inputs(quantity=0, available_quantity=0, cash=1000, risk_percent=10, max_weight_percent=5), 100, True)
    x = result["additional_quantity"]
    assert x * result["buy_cost_per_coin"] <= 1000
    post_equity = 1000 - x * (result["buy_cost_per_coin"] - 100)
    assert x * 100 / post_equity <= 0.05 + 1e-10


@pytest.mark.parametrize("changes,eligible", [({"exit_price": 110}, True), ({}, False), ({"available_quantity": 0.5}, True), ({"risk_percent": 0.1}, True)])
def test_invalid_exit_unready_locked_or_exhausted_risk_blocks_additions(changes, eligible):
    result = assess_portfolio(inputs(**changes), 100, eligible)
    assert result["additional_quantity"] == 0
    assert result["reasons"]
    assert result["max_sell_quantity"] <= 1
