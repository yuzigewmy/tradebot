# 十五分钟永续策略与使用模式

2026 年 11 月 18 日查阅官方代码和文档，并从旧版 OKX 逐笔账本重算。旧版 722 笔中的止损距离中位数为入场价的 0.0932%，531 笔低于旧规则用于估算的 0.14% 往返费和滑点。其手续费为 95.6020 USDT，剔除手续费后按原成交和仓位计算净盈利约 0.0060 USDT。

| 参考 | 采用的做法 |
|---|---|
| Freqtrade（57,003 星）与 Freqtrade Strategies（5,663 星） | 用高周期趋势配合回踩收盘与 ATR 止损。严格区分信号触发线和此前的比较窗口，验证回测和实时用同样的历史长度。参考 [TrendRiderStrategy](https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/TrendRiderStrategy.py)、[前视检查](https://www.freqtrade.io/en/stable/lookahead-analysis/)和[回测成本](https://www.freqtrade.io/en/stable/backtesting/)。 |
| Curtis Faith 的海龟规则 | 用此前区间高低点识别突破，用波动幅度衡量止损距离。在本项目里按十五分钟计算区间，并加入成交量与成本门槛。[原始规则](https://www.tradingblox.com/originalturtles/originalturtlerules.htm) |
| Hummingbot（21,249 星） | 将持仓截止与止盈和止损并列处理。[PositionExecutor](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/executors/position_executor/data_types.py) |
| NautilusTrader（31,655 星） | 策略、证据和执行分开。开仓用当时的买卖一档，退出用按时间排序的成交，回放逐仓资金账本。[回测文档](https://nautilustrader.io/docs/latest/concepts/backtesting/) |

星数为调研当天的 GitHub API 快照。

新版先看一小时 EMA20 与 EMA50 的排列、短均线近三根的斜率和 ADX。候选要求 ADX ≥ 20，且十五分钟收盘方向一致。十五分钟触发使用其中一项形态：

- 区间突破。收盘越过此前二十根的高点或低点至少 0.05 倍 ATR。触发成交量至少为此前二十根均量的 1.25 倍。
- 趋势回踩。价格在当根测试 EMA20，收盘重回趋势一侧并越过上一根的高点或低点。触发成交量至少为此前二十根均量的 1.1 倍。

止损按相应结构再加 0.25 倍 ATR 缓冲，实际距离须落在 1.25–3 倍十五分钟 ATR 约束内。预估往返手续费、出场滑点、当前价差和不利资金费合计最多占结构止损距离的 25%。止盈按估计净风险的两倍设置。

普通看盘使用行情观察。复核并推送模式从十五分钟候选开始，经过预算筛选后交给模型，模型确认后按最新报价重新检查偏离、成本与价格区间。入场窗口到下一根一分钟收盘结束。同一合约发布后冷却一小时。模拟持仓遇到止盈、止损、标记价强平或触发时间起六小时后的首笔成交时按事件出场。观察、发布、模型等待和价格拒绝都可在永续页查看。

## 复跑

旧版用 `--profile v1` 读取原规则、Skill 和历史研判记录。下列命令使用新版已缓存的盘口、秒线和模型回复，重建同一窗口的资金账本。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m spot_agent.perpetual_backtest --profile v2 --start 2026-08-18T08:00:00+08:00 --end 2026-11-18T08:00:00+08:00 --cache data/okx-history --out artifacts/perpetual-backtest-v2-2026-11-18 --stage report
```

运行 `perpetual_paper_live --profile v2` 开启独立的模拟账本。网页和模拟账户读 OKX 公共行情，策略与回放共用价格规则、模型复核和持仓退出规则。开仓预算都采用已知上期资金费估算，本期结算仍用该期真实费率。回放中按当次模型耗时定位后续盘口，并在时间轴上应用冷却规则。模型费用和交易手续费单独统计。
