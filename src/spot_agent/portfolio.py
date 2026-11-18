"""Spot-only position scenarios. No order execution or inferred cost basis."""


def assess_portfolio(position, price, eligible):
    quantity = position.quantity
    equity = position.cash + quantity * price
    fee = position.fee_percent / 100
    slippage = position.slippage_percent / 100
    buy_cost = price * (1 + slippage) * (1 + fee)
    exit_proceeds = position.exit_price * (1 - slippage) * (1 - fee)
    budget = equity * position.risk_percent / 100
    weight_limit = position.max_weight_percent / 100
    reasons = []
    if not eligible:
        reasons.append("数据质量、市场状态或多周期条件不满足，暂停新增仓位。")
    if position.exit_price >= price:
        reasons.append("退出参考价须低于当前价格，才能估计新增多头风险。")
    if position.available_quantity < quantity:
        reasons.append("部分持仓不可卖出，不能假设全部可按退出价平仓，暂停新增。")
    existing_risk = quantity * max(0, price - exit_proceeds)
    risk_per_coin = buy_cost - exit_proceeds
    remaining = max(0, budget - existing_risk)
    cash_cap = position.cash / buy_cost
    weight_cap = max(0, (weight_limit * equity - quantity * price) / (price + weight_limit * (buy_cost - price)))
    risk_cap = remaining / risk_per_coin if position.exit_price < price else 0
    if remaining == 0:
        reasons.append("已有持仓已占满本次资金的风险预算。")
    if cash_cap == 0:
        reasons.append("可用现金为0。")
    if weight_cap == 0:
        reasons.append("已达到单币权重上限。")
    additional = 0 if reasons else min(risk_cap, cash_cap, weight_cap)
    sale_cost = price - price * (1 - slippage) * (1 - fee)
    trim = max(0, (quantity * price - weight_limit * equity) / (price - weight_limit * sale_cost))
    return {
        "scope": "仅包含所填现金和当前币种，未包含其他资产",
        "price": price, "equity": equity, "quantity": quantity,
        "cash": position.cash, "average_cost": position.average_cost,
        "cost_complete": position.average_cost is not None,
        "unrealized_pnl": None if position.average_cost is None else quantity * (price - position.average_cost),
        "current_weight_percent": quantity * price / equity * 100 if equity > 0 else None,
        "max_weight_percent": position.max_weight_percent, "risk_percent": position.risk_percent,
        "exit_price": position.exit_price, "risk_budget": budget,
        "existing_risk": existing_risk, "remaining_risk_budget": remaining,
        "risk_per_new_coin": risk_per_coin, "buy_cost_per_coin": buy_cost,
        "additional_quantity": additional, "max_sell_quantity": position.available_quantity,
        "trim_to_weight_quantity": min(position.available_quantity, trim),
        "reasons": reasons,
        "note": "数量为含费用/滑点假设的情景上限，未按交易所精度和最小金额处理；退出价不保证成交。",
    }
