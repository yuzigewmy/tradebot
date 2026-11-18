# 加密研究台

本地 BTC/ETH 研究助手，提供现货分析和 OKX USDT 永续开仓研判。现货按同一截止时间分析日线、4H、1H，生成中文报告、条件情景和手工持仓评估。

## 启动

```powershell
Set-Location D:\crypto-spot-agent
.\start.ps1
```

打开 `http://127.0.0.1:8765`。服务只监听本机。若换端口，执行 `.\start.ps1 -Port 8766`。

在其他机器重建环境时，使用 Python 3.13：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m pytest -q
```

## 永续开仓研判

- 打开 `/perpetual`，分别启动 BTC 或 ETH。行情观察模式显示收盘分析，复核并推送模式在通过报价与成本门槛后调用模型。关闭网页后监控继续运行。重启服务后重新启动监控。
- OKX 通过 WebSocket 更新 1m、15m、1H 成交 K 线。新版用一小时判断趋势，十五分钟识别区间突破和趋势回踩，一分钟跟踪成交及入场时效。模型根据计算证据确认初始候选，程序重新读取盘口并校验价格与成本。
- 信号包含方向、入场参考、止盈、止损和持仓截止。入场窗口为触发收盘后的第一分钟。发布记录保存在 `data/perpetual-signals.jsonl`，绑定微信后可以收到新信号。后台模拟使用相同的价格和持仓规则。
- 调研、模式和复跑命令见[永续策略升级](docs/perpetual-v2.md)。策略参数与模型复核提示词见 [15m-policy.json](skills/perpetual-entry/15m-policy.json) 和 [15m-review.md](skills/perpetual-entry/15m-review.md)。

历史回测与实时模拟使用同一逐仓账户，默认 BTC/ETH 共用 100 USDT、25×、单笔计划止损风险为账户权益的 1%。回测读取 OKX 公开成交、盘口、标记价和已结算资金费率，并实际调用策略 Skill。运行命令、执行明细和数据边界见[永续回测与实时模拟](docs/perpetual-backtest.md)。

## 微信助手

在研究台顶部打开「微信」，用手机扫码确认，再给机器人发一条私聊。可以直接交谈、写作、查询公开资料，也可以让助手比较 BTC 和 ETH 的现货或永续走势、查看资金费、查询信号以及解释本地回测。助手保留本次服务运行中的最近八轮对话。编辑 [微信助手提示词](prompts/wechat-assistant.md) 后，下一条问答读取新内容。

永续监控启动后，策略和模型复核通过的新信号会自动推送给绑定账号。推送沿用发布记录里的方向、开仓参考、止盈、止损和有效期。微信绑定会在本机重启后恢复，永续监控的启动方式见上文。

## 现货分析与模型配置

- 真实行情从所选交易所获取；无网络时可显式选择合成演示。CSV 历史回放只分析实际导入的周期。三类来源分别标记。
- 持仓只含当前币种与所填现金。调整风险参数后可用当前报告快照评估，无需重新请求行情。
- 深度研判使用 Responses API。项目的 `.env` 保存本机接口设置，模板见 `.env.example`。模型、推理强度、服务档位分别由 `SPOT_LLM_MODEL`、`SPOT_LLM_REASONING_EFFORT`、`SPOT_LLM_SERVICE_TIER` 配置。无配置时可使用全部确定性分析。
- 本机配置使用 `gpt-6.2-sol` / `high`。该网关实测接受 `ultrafast` 并返回同名档位，`priority` 和 `fast` 探测则返回 `default`。应用显示网关实际返回的档位，不根据单次测试承诺提速。
- 深度研判会将用户问题和所调用的证据发送到配置的模型服务。只有调用持仓工具时才发送持仓情景。请求设置 `store: false`，第三方网关自身的数据保留以其规则为准。密钥仅供服务器使用，不会放入网页、报告或 Git。

现货快照保存在 `data/reports/`，可导出 Markdown。参考[现货实施方案](docs/implementation-plan.md)和[调研记录](docs/research.md)。历史模拟收益只描述指定样本和撮合假设。
