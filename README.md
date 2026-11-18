# 现货研究台

本地 BTC/ETH 现货分析助手。按同一截止时间读取交易所已收盘 K 线，计算日线、4H、1H 的技术指标和价格结构，生成带证据的中文报告、条件情景和手工持仓评估。

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

## 分析与模型

- 真实行情从所选交易所获取；无网络时可显式选择合成演示。CSV 历史回放只分析实际导入的周期。三类来源分别标记。
- 持仓只含当前币种与所填现金。调整风险参数后可用当前报告快照评估，无需重新请求行情。
- 深度研判使用 Responses API。项目的 `.env` 保存本机接口设置，模板见 `.env.example`。模型、推理强度、服务档位分别由 `SPOT_LLM_MODEL`、`SPOT_LLM_REASONING_EFFORT`、`SPOT_LLM_SERVICE_TIER` 配置。无配置时可使用全部确定性分析。
- 本机配置使用 `gpt-6.2-sol` / `high`。该网关实测接受 `ultrafast` 并返回同名档位，`priority` 和 `fast` 探测则返回 `default`。应用显示网关实际返回的档位，不根据单次测试承诺提速。
- 深度研判会将用户问题和所调用的证据发送到配置的模型服务。只有调用持仓工具时才发送持仓情景。请求设置 `store: false`，第三方网关自身的数据保留以其规则为准。密钥仅供服务器使用，不会放入网页、报告或 Git。

快照保存在 `data/reports/`，可导出 Markdown。参考[详细实施方案](docs/implementation-plan.md)和[调研记录](docs/research.md)。首版未验证策略盈利能力，无自动下单。回测、连续模拟和多币组合账本为后续阶段。
