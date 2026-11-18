"""Transparent conditional scenarios, not backtested performance claims."""


def build_strategy(analyses):
    directions = [a["direction"] for a in analyses.values()]
    ready = all(a["ready"] for a in analyses.values()) and set(analyses) == {"1h", "4h", "1d"}
    all_up = all(d == "up" for d in directions)
    all_down = all(d == "down" for d in directions)
    conflict = "up" in directions and "down" in directions
    if not ready:
        state, strength = "数据条件不足", "不足"
    elif conflict:
        state, strength = "多周期方向冲突", "弱"
    elif all_up:
        state, strength = "多周期上升排列", "较强"
    elif all_down:
        state, strength = "多周期下降排列", "较强"
    else:
        state, strength = "趋势交错 / 区间观察", "中等"
    execution = analyses["1h"] if "1h" in analyses else next(iter(analyses.values()))
    reference = analyses["4h"] if "4h" in analyses else execution
    price = execution["price"]
    support = reference["zones"]["support"]
    resistance = reference["zones"]["resistance"]
    observations = [f"{tf}：{a['trend_label']}，结构 {a['structure']}。" for tf, a in analyses.items()]
    counter = []
    if conflict:
        counter.append("高低周期方向相反，不能把短线反弹直接视为高周期反转。")
    if not all_up:
        counter.append("并非全部周期上升排列，趋势回踩候选尚缺一致方向。")
    rsi = execution["indicators"]["rsi14"]
    if rsi is not None and (rsi >= 70 or rsi <= 30):
        counter.append("执行周期处于RSI极值区；趋势延续与短期回撤风险并存。")
    if all_up:
        counter.append("均线是滞后指标；新近已确认低点失守将削弱当前上升判断。")
    scenarios = [
        {
            "name": "上升趋势：回踩观察", "status": "需多周期上升并完成回踩确认",
            "trigger": "多周期上升排列，参考周期回踩均线或已确认支撑，低点结构保持；执行周期重新收回短均线后再评估。",
            "invalidation": "参考周期已确认更高低点失守，或多周期上升排列被破坏。",
            "reference_zone": support, "evidence_ids": [f"{reference['timeframe']}:trend", f"{reference['timeframe']}:structure"],
        },
        {
            "name": "上行：突破后回踩", "status": "等待条件确认",
            "trigger": "参考周期收盘突破阻力价区，量比至少1.2；之后回踩守住，再由1H收盘确认。",
            "invalidation": "突破后收回阻力价区下方，或已确认回踩低点失守。",
            "reference_zone": resistance, "evidence_ids": [f"{reference['timeframe']}:structure", f"{execution['timeframe']}:volume"],
        },
        {
            "name": "横盘：区间观察", "status": "需确认区间与低趋势强度",
            "trigger": "支撑与阻力两侧反复有效，ADX低于20；观察支撑附近的价格确认，不机械抄底。",
            "invalidation": "参考周期收盘离开区间并延续，或波动显著扩张。",
            "reference_zone": support, "evidence_ids": [f"{reference['timeframe']}:structure", f"{reference['timeframe']}:trend"],
        },
        {
            "name": "下行：失守与敞口复核", "status": "条件式风险观察",
            "trigger": "参考周期收盘跌破已确认支撑价区，反抽未收复；按配置的退出价复核持仓风险。",
            "invalidation": "收复支撑并形成新的已确认更高低点，随后重新评估。",
            "reference_zone": support, "evidence_ids": [f"{reference['timeframe']}:structure", f"{execution['timeframe']}:volatility"],
        },
    ]
    if support is None or resistance is None:
        counter.append("缺少完整支撑或阻力，相关情景只保留观察条件，不能据此计算完整区间收益风险比。")
    risk_reward = None
    if support is not None and resistance is not None:
        if support["low"] < price < resistance["low"]:
            risk_reward = (resistance["low"] - price) / (price - support["low"])
        else:
            counter.append("当前价格已越过参考周期原价区，原区间收益风险比不适用。")
    return {
        "state": state, "evidence_strength": strength,
        "eligible_to_plan_addition": ready and all_up,
        "action": "等待回踩或突破后确认；可评估新增上限" if ready and all_up else "观察 / 暂停新增，已有持仓按退出条件评估",
        "observations": observations, "counter_evidence": counter, "scenarios": scenarios,
        "reference_timeframe": reference["timeframe"], "support": support, "resistance": resistance,
        "gross_zone_reward_risk": risk_reward,
        "limitations": "情景条件尚未经过样本外收益验证；证据强弱不是上涨概率；价区收益风险比为未扣成本的几何参考。",
    }
