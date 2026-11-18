from datetime import datetime, timezone


def utc(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def markdown(report):
    strategy = report["consensus"]
    source = {"live": "交易所真实现货行情", "synthetic": "合成演示数据", "import": "CSV历史导入回放"}[report["source"]]
    lines = [
        f"# {report['symbol']} 现货技术分析", "",
        f"数据：{source} · {report['exchange']} · 截止 {utc(report['as_of'])}",
        f"快照：{report['snapshot_id']} · 规则：{report['rule_version']}", "",
        "## 核心判断", "", f"{strategy['state']}，证据强度：{strategy['evidence_strength']}。",
        strategy["action"], "", "## 多周期分析", "",
    ]
    for timeframe, analysis in report["timeframes"].items():
        lines += [f"### {timeframe} · {analysis['trend_label']}", "", f"最新已收盘：{utc(analysis['last_closed_at'])}；收盘价：{analysis['price']:,.4f} USDT。", ""]
        for evidence in analysis["evidence"]:
            lines += [f"- **{evidence['title']}**：{evidence['text']} [{evidence['id']}]" ]
        lines += ["", *[f"提示：{text}" for text in analysis["cautions"]], ""]
    lines += ["## 反证", "", *[f"- {text}" for text in strategy["counter_evidence"]], "", "## 条件情景", ""]
    for scenario in strategy["scenarios"]:
        lines += [f"### {scenario['name']}", "", f"确认：{scenario['trigger']}", f"失效：{scenario['invalidation']}", ""]
    if report["portfolio"] is not None:
        portfolio = report["portfolio"]
        lines += [
            "## 持仓与风险", "", portfolio["scope"], "",
            f"本次资金：{portfolio['equity']:,.2f} USDT；风险预算：{portfolio['risk_budget']:,.2f} USDT。",
            f"已有持仓估计风险：{portfolio['existing_risk']:,.2f} USDT；剩余预算：{portfolio['remaining_risk_budget']:,.2f} USDT。",
            f"新增数量情景上限：{portfolio['additional_quantity']:.8f}；最多可卖：{portfolio['max_sell_quantity']:.8f}。",
            *portfolio["reasons"], portfolio["note"], "",
        ]
    lines += ["## 方法和限制", "", strategy["limitations"], "", "指标由TA-Lib及确定性规则计算；没有模型深度研判时，本报告仍为完整的规则分析。具体参数保存在JSON快照中。", "K线不能证明主力资金流；止损不保证成交；本系统没有实盘下单功能。", ""]
    return "\n".join(lines)
