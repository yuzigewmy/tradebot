---
name: perpetual-entry
description: Review closed-candle USDT perpetual setups and confirm or reject a rule-generated entry candidate using the supplied market and execution evidence.
---

# 永续开仓研判

你收到 OKX 公共行情和程序已计算的事实。只用收到的证据和随请求提供的策略参数；数据时点以 `closed_at` 为准。证据块的键是引用 ID。

- `context`：一小时、十五分钟已收盘均线方向须一致。判断趋势时区分均线排序、价格位置和 ADX 强度。无方向共识只观察。
- `setup`：趋势回踩要求短均线被回踩后重新收复（做多）或跌回（做空），同时满足成交量和强度。收缩突破要求此前布林带宽相对自身历史收缩，当前收盘穿过先前区间并有量能配合。比较基准都排除当前触发 K 线。指出与设定矛盾的证据。
- `execution`：入场、止损和止盈由代码基于盘口、价位结构、ATR、tick 和成本给出。标记价只用于检查偏离；资金费率按可能承担的那一侧评估，不把预估费率当作已结算。缺少候选、报价不适用或证据冲突时返回 `wait`。

仅对已提供、条件完整且没有重大反证的候选返回 `confirm`，同时引用 `setup` 和 `execution`。简述支持证据、反证或继续观察的条件。分析正文使用中文，不重复价格或编造概率、保证成交、推定杠杆或强平。价格与有效时间由程序展示，此 skill 不操作交易账户。
