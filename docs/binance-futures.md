# 币安 USDⓈ-M 永续自动下单

在运行 Tradebot 的本机或服务器上编辑 `.env`，保存后重启服务。`BINANCE_FUTURES_MODE` 决定永续监控使用的数据和订单环境。

| 值 | 启动后的行为 |
| --- | --- |
| `off` | 使用原来的 OKX 研究和模拟监控，也是默认值 |
| `quotes` | 启动 BTC、ETH 的币安公开行情监控，页面和微信查询使用币安正式环境的报价，不需要密钥 |
| `demo` | 使用币安 Demo 的行情和账户，启动后自动执行两套策略 |
| `live` | 使用币安正式环境的行情和账户，启动后自动执行两套策略 |

`demo` 与 `live` 使用各自的 HMAC API 密钥。由账户持有人将密钥填入运行实例的私有 `.env` 或由服务管理器注入同名环境变量。

```dotenv
BINANCE_FUTURES_MODE=demo
BINANCE_FUTURES_API_KEY=
BINANCE_FUTURES_API_SECRET=
```

为合约专用账户启用**读取**和 **USDⓈ-M 永续交易**权限，关闭提现权限。正式环境应允许服务机请求实际使用的公网出口 IP。如果请求通过代理发出，白名单填写代理实际出口 IP。账户需使用**单向持仓**、**单资产模式**并关闭自动追加保证金。用于自动交易的 BTCUSDT、ETHUSDT 不应有其他策略或手动仓位、挂单。每次入场读取当时 U 本位合约钱包的 USDT 余额，取 25% 作为逐仓保证金预算，使用 30 倍杠杆计算下单数量。下单数量按币安的数量步长向下取整。可用 USDT 不足、数量或名义额低于下单门槛时，跳过该笔入场。

BTC 使用多周期均线策略，ETH 使用 30 分钟波动收缩做空。K 线、买卖一档、最新成交价、标记价格和资金费率来自当前币安环境。策略只评估已收盘 K 线。币安交易模式启动时直接加载这两套策略。策略参数来自此前的 OKX 回测，收益结果不能直接移用于币安。

策略触发后，在入场窗内发 IOC 限价单。IOC 指立即成交允许的部分并撤销余量。每个合约最多管理一仓，使用 USDⓈ-M 官方 SDK 设置逐仓和杠杆。币安确认入场成交后，执行器先挂止损再挂止盈，保护单只关闭该合约已有仓位。止盈止损按币安最新合约成交价触发，实际平仓价以撮合回报为准。达到持仓截止时间或确认保护单无法挂设时，执行器提交只减仓平仓单。子 agent 的提前止盈止损判断通过微信提醒，到期和保护失败退出按固定规则执行。资金费以币安实际结算为准，策略按发布信号时的结算时间预留退出窗口。

页面和微信会显示交易环境以及入场、保护挂单和结案状态。结案盈亏依据币安成交、已入账的手续费和资金费。非 USDT 手续费单列。页面的「停止开新仓」只停止该币种产生新信号，已开仓位继续追踪。页面的「行情观察」也可暂停新入场。SQLite 账本随 `data/` 一起保存。使用相同币安账户的执行服务应只运行一份。迁移时保留对应模式的账本和微信状态。如果切到其它运行模式，先在原模式确认已无在管持仓或挂单。

验收时先用 `quotes` 检查 `/api/perpetual/BTC-USDT-SWAP`、`/api/perpetual/ETH-USDT-SWAP` 的 `exchange`、`charts` 和 `quote`。再配置 Demo 密钥，用真实 Demo 回报验证账户签名、开仓、保护、平仓和微信。微信若尚未绑定，可扫码并给机器人发一条私聊。`/api/health` 显示当前执行环境和交易状态。币安 `order/test` 接口可检查参数，验证成交和保护单要使用 Demo 撮合。

参考：[币安官方 Python SDK](https://github.com/binance/binance-connector-python)、[USDⓈ-M REST 说明](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)、[开仓与平仓](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order)、[止盈止损条件单](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order)。
