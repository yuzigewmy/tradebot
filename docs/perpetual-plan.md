# USDT 永续信号：调研与实施

调研日期：2026-11-18。保留原现货研究台，另设永续页面。首版只使用 OKX BTC/ETH 线性 USDT 永续公共行情。

| 官方项目 | GitHub Stars 调研快照 | 本轮采用的做法 |
|---|---:|---|
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 56,996 | [前视检查](https://github.com/freqtrade/freqtrade/blob/develop/docs/lookahead-analysis.md)用于核对历史前缀和滚动窗口；[合约文档](https://github.com/freqtrade/freqtrade/blob/develop/docs/leverage.md)区分资金费率、止损和强平边界。 |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | 31,651 | 按事件时序把行情、信号和后续成交分开；研究与实时监控共用规则。 |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | 21,247 | [TripleBarrierConfig](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/executors/position_executor/data_types.py)区分止损、止盈和时间条件；本轮实现的是开仓信号到期，不模拟开仓后的时间退出。 |
| [Jesse](https://github.com/jesse-ai/jesse) | 8,818 | 多周期、多币种研究，以及滚动样本外和蒙特卡洛的研究思路。 |
| [Freqtrade Strategies](https://github.com/freqtrade/freqtrade-strategies) / [Passivbot](https://github.com/enarjord/passivbot) | 5,663 / 2,181 | 参考趋势与波动条件、合约策略与执行分离；不采用依赖持续补仓、网格成交及仓位模型的策略。 |

星数是 GitHub 官方页面读数，随时间变化。表中是项目与设计参考，没有移植策略源码，也没有验证其收益。

- 行情：OKX [成交 K 线](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-ws-candlesticks-channel) 1m / 15m / 1H，连接时 REST 补历史。只在 `confirm=1` 时推进策略。105 秒前闭合的高周期 K 线可以使用，但晚于决策时点的 K 线不可以；在周期交界处等对应高周期收盘确认。断线补齐历史后从下一根开始，不补发旧信号。
- 规则：1H 和 15m 定方向，1m 确认回踩恢复或波动收缩后的放量突破。精确阈值在 [policy.json](../skills/perpetual-entry/policy.json)，解释规则在 [SKILL.md](../skills/perpetual-entry/SKILL.md)。窗口和量比基准排除触发 K 线。
- 报价：交易所成交用于形态与退出参考；盘口用于估算入场；标记价用于基差检查；资金费率按可能跨过一次结算的情景计入成本。无账户数据，不估算强平或杠杆仓位。
- 研判：本地服务主动连接行情，每根 1m 收盘计算。默认“关键变化”触发模型，含首次评估、15m 收盘、方向或波动状态变化及规则候选；可以显式切换成每根 1m。模型收到策略 skill、参数和本次计算证据，同一合约只处理最新待分析事件。模型只能确认或等待。候选未通过复核、已到期或报价不再适用时不发布。
- 信号：候选给出方向、条件、精确到交易所 tick 的入场、止损、止盈、费用后估计收益风险比、价格来源和时效。确认信号保存候选、原始证据和模型结论。保留信号历史供复盘；无交易权限。
- 验证：必要项为跨周期的确认边界、前视窗口、双向价格与成本计算、复核绑定与过期、真实订阅及浏览器。后续收益评估须使用实际时间顺序、费用、资金费率和样本外窗口；本次实现只确认信号系统工作。
