"""Explicit TA-Lib parameters and causal, confirmed market structure.

Source: https://github.com/TA-Lib/ta-lib-python#function-api
"""

import math

import numpy as np
import talib

from .contracts import TIMEFRAMES

PARAMETERS = {
    "ema": [20, 50, 200], "rsi": 14, "adx": 14, "atr": 14,
    "macd": [12, 26, 9], "bollinger": [20, 2, 2],
    "volume_baseline": 20, "swing_left_right": [2, 2],
}


def json_number(value):
    """TA-Lib's warmup NaN is represented as JSON null, never as zero."""
    return None if math.isnan(value) else float(value)


def confirmed_swings(rows, duration):
    points = []
    for i in range(2, len(rows) - 2):
        for kind, column in [("high", 2), ("low", 3)]:
            price = rows[i][column]
            left = [r[column] for r in rows[i - 2:i]]
            right = [r[column] for r in rows[i + 1:i + 3]]
            is_pivot = price >= max(left) and price > max(right) if kind == "high" else price <= min(left) and price < min(right)
            if is_pivot:
                points.append({
                    "kind": kind, "price": price,
                    "occurred_at": int(rows[i][0]),
                    "confirmed_at": int(rows[i + 2][0] + duration),
                })
    return points


def price_zones(swings, price, atr):
    tolerance = atr * 0.35 if atr is not None else price * 0.002
    groups = []
    for point in sorted(swings[-60:], key=lambda s: s["price"]):
        if groups and point["price"] - groups[-1][-1] <= tolerance:
            groups[-1].append(point["price"])
        else:
            groups.append([point["price"]])
    zones = [{"price": float(np.mean(g)), "low": min(g) - tolerance, "high": max(g) + tolerance, "touches": len(g)} for g in groups]
    support = [z for z in zones if z["high"] < price]
    resistance = [z for z in zones if z["low"] > price]
    return {"support": support[-1] if support else None, "resistance": resistance[0] if resistance else None}


def analyze_frame(frame):
    rows = frame.rows[frame.quality["tail_start"]:]
    data = np.array(rows, dtype=float)
    opening, high, low, close, volume = (data[:, i] for i in range(1, 6))
    price = float(close[-1])
    ema20, ema50, ema200 = [talib.EMA(close, timeperiod=p) for p in PARAMETERS["ema"]]
    macd, signal, histogram = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    upper, middle, lower = talib.BBANDS(close, timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)
    atr = talib.ATR(high, low, close, timeperiod=14)
    rsi = talib.RSI(close, timeperiod=14)
    adx = talib.ADX(high, low, close, timeperiod=14)
    plus_di = talib.PLUS_DI(high, low, close, timeperiod=14)
    minus_di = talib.MINUS_DI(high, low, close, timeperiod=14)
    obv = talib.OBV(close, volume)
    baseline = float(volume[-21:-1].mean()) if len(rows) >= 21 else 0
    indicators = {
        "ema20": json_number(ema20[-1]), "ema50": json_number(ema50[-1]), "ema200": json_number(ema200[-1]),
        "rsi14": json_number(rsi[-1]), "adx14": json_number(adx[-1]),
        "plus_di": json_number(plus_di[-1]), "minus_di": json_number(minus_di[-1]),
        "macd": json_number(macd[-1]), "macd_signal": json_number(signal[-1]), "macd_histogram": json_number(histogram[-1]),
        "atr14": json_number(atr[-1]), "atr_percent": json_number(atr[-1] / price * 100),
        "bb_upper": json_number(upper[-1]), "bb_middle": json_number(middle[-1]), "bb_lower": json_number(lower[-1]),
        "bb_width_percent": json_number((upper[-1] - lower[-1]) / middle[-1] * 100),
        "volume_ratio": float(volume[-1] / baseline) if baseline > 0 else None,
        "obv": float(obv[-1]), "obv_change5": float(obv[-1] - obv[-6]) if len(rows) >= 6 else None,
        "ema20_slope5_percent": json_number((ema20[-1] / ema20[-6] - 1) * 100) if len(rows) >= 6 else None,
        "ema20_distance_percent": json_number((price / ema20[-1] - 1) * 100),
    }
    swings = confirmed_swings(rows, TIMEFRAMES[frame.timeframe])
    highs = [s for s in swings if s["kind"] == "high"]
    lows = [s for s in swings if s["kind"] == "low"]
    structure = "不足两个已确认高低点"
    if len(highs) >= 2 and len(lows) >= 2:
        high_structure = "HH" if highs[-1]["price"] > highs[-2]["price"] else "LH" if highs[-1]["price"] < highs[-2]["price"] else "EH"
        low_structure = "HL" if lows[-1]["price"] > lows[-2]["price"] else "LL" if lows[-1]["price"] < lows[-2]["price"] else "EL"
        structure = high_structure + "/" + low_structure
    zones = price_zones(swings, price, indicators["atr14"])
    direction = "unknown"
    if indicators["ema200"] is not None:
        direction = "up" if price > ema20[-1] > ema50[-1] > ema200[-1] else "down" if price < ema20[-1] < ema50[-1] < ema200[-1] else "mixed"
    trend_label = {"up": "上升排列", "down": "下降排列", "mixed": "均线交错", "unknown": "趋势数据不足"}[direction]
    ready = len(rows) >= 500 and frame.quality["gap_count"] == 0 and not frame.quality["stale"]
    patterns = []
    if talib.CDLENGULFING(opening, high, low, close)[-1] != 0:
        patterns.append("看涨吞没" if talib.CDLENGULFING(opening, high, low, close)[-1] > 0 else "看跌吞没")
    if talib.CDLHAMMER(opening, high, low, close)[-1] != 0:
        patterns.append("锤头线")
    if talib.CDLDOJI(opening, high, low, close)[-1] != 0:
        patterns.append("十字线")

    def fmt(value, digits=2):
        return "预热不足" if value is None else f"{value:,.{digits}f}"

    momentum = "预热不足"
    if indicators["rsi14"] is not None:
        momentum = "处于超买区，强趋势中可持续，不单独构成卖出" if rsi[-1] >= 70 else "处于超卖区，需价格确认，不单独构成买入" if rsi[-1] <= 30 else "处于中间区，结合结构与MACD判断"
    at = frame.quality["last_closed_at"]
    evidence = []
    def add(category, title, text, values):
        evidence.append({"id": f"{frame.timeframe}:{category}", "timeframe": frame.timeframe, "at": at, "category": category, "title": title, "text": text, "values": values})

    add("trend", "趋势与强度", f"{trend_label}；EMA20/50/200={fmt(indicators['ema20'])}/{fmt(indicators['ema50'])}/{fmt(indicators['ema200'])}；ADX14={fmt(indicators['adx14'])}。ADX只衡量强度；均线与MACD相关，不能重复计为独立证据。", {k: indicators[k] for k in ["ema20", "ema50", "ema200", "adx14", "plus_di", "minus_di"]})
    zone_text = "；".join(name + ("未识别" if zone is None else f"{zone['low']:,.2f}–{zone['high']:,.2f}") for name, zone in [("支撑", zones["support"]), ("阻力", zones["resistance"])])
    add("structure", "价格结构与关键价区", f"已确认结构 {structure}；{zone_text}。摆动点在右侧两根收盘后才可用；价区并不保证反转。", {"structure": structure, **zones})
    add("momentum", "动量与冲突", f"RSI14={fmt(indicators['rsi14'])}，{momentum}。MACD/信号/柱={fmt(indicators['macd'], 4)}/{fmt(indicators['macd_signal'], 4)}/{fmt(indicators['macd_histogram'], 4)}。", {k: indicators[k] for k in ["rsi14", "macd", "macd_signal", "macd_histogram"]})
    add("volatility", "波动与风险距离", f"ATR14={fmt(indicators['atr14'])}，占价格 {fmt(indicators['atr_percent'])}%；布林带宽 {fmt(indicators['bb_width_percent'])}%。ATR描述历史波动，不保证未来风险上限。", {k: indicators[k] for k in ["atr14", "atr_percent", "bb_upper", "bb_middle", "bb_lower", "bb_width_percent"]})
    add("volume", "量价配合", f"本根相对前20根均量 {fmt(indicators['volume_ratio'])} 倍；OBV近5根变化 {fmt(indicators['obv_change5'])}。这些是成交量统计，不代表主力净流入。", {k: indicators[k] for k in ["volume_ratio", "obv", "obv_change5"]})
    add("pattern", "K线形态", "、".join(patterns) + "；需结合价区和下一根确认。" if patterns else "最新已收盘K线未出现本版检测的吞没、锤头、十字形态。", {"patterns": patterns})
    cautions = []
    if len(rows) < 500:
        cautions.append(f"连续历史仅{len(rows)}根，未达到500根稳定性观察窗口，新增仓位建议停用。")
    if frame.quality["gap_count"]:
        cautions.append("历史存在缺口；指标只计算最后连续片段，未插补。")
    if frame.quality["stale"]:
        cautions.append("最新应收盘周期缺失，行情已过期。")
    if frame.source == "synthetic":
        cautions.append("合成演示数据，所有价格、形态与持仓计算仅演示。")
    chart_series = {name: [json_number(v) for v in values[-180:]] for name, values in [("ema20", ema20), ("ema50", ema50), ("ema200", ema200), ("rsi", rsi), ("macd_histogram", histogram)]}
    return {
        "timeframe": frame.timeframe, "as_of": frame.as_of, "last_closed_at": at,
        "quality": frame.quality, "ready": ready, "price": price,
        "change_percent": float((close[-1] / close[-2] - 1) * 100) if len(rows) > 1 else None,
        "direction": direction, "trend_label": trend_label, "structure": structure,
        "indicators": indicators, "zones": zones, "swings": swings[-30:], "patterns": patterns,
        "evidence": evidence, "cautions": cautions,
        "chart": {"rows": rows[-180:], "series": chart_series},
    }
