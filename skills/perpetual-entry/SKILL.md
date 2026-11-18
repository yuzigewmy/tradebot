---
name: perpetual-entry
description: Review closed-candle USDT perpetual setups and confirm or reject a rule-generated entry candidate using the supplied market and execution evidence.
---

# 永续开仓研判

你收到 OKX 公共行情和程序已计算的事实。只用收到的证据和随请求提供的策略参数；数据时点以 `closed_at` 为准。证据块的键是引用 ID。

- `context`：一小时、十五分钟已收盘均线方向须一致。判断趋势时区分均线排序、价格位置和 ADX 强度。无方向共识只观察。
- `setup`：趋势回踩要求一分钟收盘重新收复短均线（做多）或跌回（做空）。回踩强度只看 `pullback_adx_15m_pass`，成交量看 `pullback_volume_1m_pass`。收缩突破要求先前布林带收缩、收盘穿越前区间，且 `breakout_volume_1m_pass`。比较基准排除当前触发 K 线。直接引用程序的布尔判定，不另用其他周期重算门槛。
- `execution`：`candidate` 包含程序算出的入场、止损和止盈。若 `candidate` 为空，`observation_reason` 仍是可以引用的执行检查结果，此时只能返回 `wait`。标记价只用于检查偏离；资金费率按可能承担的那一侧评估，不把预估费率当作已结算。报价不适用或证据冲突时返回 `wait`。

仅对已提供、条件完整且没有重大反证的候选返回 `confirm`，同时引用 `setup` 和 `execution`。在 `analysis` 简述证据和判断，在 `counter_evidence` 写出反证或继续观察的条件；引用只放 `evidence_ids`。正文使用中文，用中文称呼周期与指标，不写阿拉伯数字或百分号，不编造概率、保证成交、推定杠杆或强平。价格与有效时间由程序展示，此 skill 不操作交易账户。
