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

普通看盘使用行情观察。复核并推送模式可选择只启用区间突破、只启用趋势回踩或同时启用。十五分钟候选通过费用预算后交给模型。模型确认后，按最新报价重新检查偏离、成本与价格区间。入场窗口到下一根一分钟收盘结束。同一合约发布后冷却一小时。模拟持仓遇到止盈、止损、标记价强平或触发时间起六小时后的首笔成交时出场。零成交量秒线不能用于成交。观察、发布、模型等待和价格拒绝都可在永续页查看。

## 同期对比

北京时间 2026 年 8 月 18 日至 11 月 18 日，每个账本均从 100 USDT 起步。采用 OKX 的 BTC 与 ETH 永续、25 倍逐仓和每笔 1% 权益的计划止损预算。入场采用当时买卖一档，计单边 0.05% 手续费和出场 0.02% 滑点，并按已结算费率计资金费。

| 运行方式 | 平仓笔数 | 净胜率 | 最大回撤 | 手续费 | 期末权益 | 净收益率 |
|---|---:|---:|---:|---:|---:|---:|
| 旧版组合 | 722 | 14.54% | 95.69% | 95.60 USDT | 4.40 USDT | −95.60% |
| 新版组合 | 145 | 33.10% | 29.01% | 15.33 USDT | 89.72 USDT | −10.28% |
| 新版仅区间突破 | 87 | 34.48% | 16.60% | 10.38 USDT | 99.69 USDT | −0.31% |
| 新版仅趋势回踩 | 69 | 30.43% | 18.03% | 6.99 USDT | 87.85 USDT | −12.15% |

单策略账本独立计算冷却、仓位和可用资金。仅区间突破按原成交价和仓位剔除手续费后净额为 +10.0769 USDT。仅趋势回踩同口径为 −5.1577 USDT。各区间已经包含在策略开发时观察的窗口中，独立时段表现尚未验证。新版所有平仓触发均与有序逐笔成交匹配。五百零一个形态候选中，有二百二十二次模型调用。二百二十一次成功调用全部确认，一次超时。正式发布仍需通过报价和冷却，模型单独带来的收益改善未验证。

## 复跑

旧版用 `--profile v1` 读取原规则、Skill 和历史研判记录。下列命令使用新版已缓存的盘口、秒线和模型回复，重建同一窗口的资金账本。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m spot_agent.perpetual_backtest --profile v2 --start 2026-08-18T08:00:00+08:00 --end 2026-11-18T08:00:00+08:00 --cache data/okx-history --out artifacts/perpetual-backtest-v2-2026-11-18 --stage report
```

单策略回测可加 `--strategy channel_breakout` 或 `--strategy trend_pullback`，并为 `--out` 指定独立目录。实时模拟使用 `perpetual_paper_live --profile v2 --strategy channel_breakout` 开启只观察突破成交的模拟账本。回放和实时模拟共用价格规则、模型复核和退出规则。开仓预算采用已知上期资金费估算，本期结算用该期真实费率。回放按实际调用的模型耗时定位后续盘口，再按时间轴应用冷却。账户收益含交易手续费、资金费和出场滑点。模型服务用量另列。
