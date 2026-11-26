import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from spot_agent.binance_market import BinanceMarket, BinanceRules
from spot_agent.binance_sdk import BinanceError, BinanceFutures, BinanceSettings

D = Decimal


def test_binance_warmup_does_not_promote_the_forming_kline():
    market = BinanceMarket(None, "demo")
    clock = 180_001

    async def wire(path, **params):
        assert (path, params["symbol"], params["interval"]) == ("/fapi/v1/klines", "BTCUSDT", "1m")
        return [[ts, "100", "102", "99", "101", "10", ts + 59_999] for ts in (60_000, 120_000, 180_000)]

    market.get = wire
    bars, forming = asyncio.run(market.candles("BTC-USDT-SWAP", "1m", clock))
    assert [row[0] for row in bars] == [60_000, 120_000]
    assert forming[0] == 180_000


def test_binance_sizes_to_exchange_step_and_checks_min_notional_at_limit():
    rules = BinanceRules(D("0.1"), D("0.001"), D("0.001"), D("10"), D("100"))
    assert rules.quantity(D("61499.9"), D("128.425925850"), sizing_price=D("61500")) == D("0.002")
    assert rules.quantity(D("49999"), D("128.425925850"), sizing_price=D("50000")) is None


def test_sdk_reads_usdt_wallet_balance_when_preparing_each_order(monkeypatch):
    sdk = BinanceFutures(BinanceSettings("demo", "test-key", "test-secret"))
    balances = iter([
        {"balance": "17.12345678", "availableBalance": "10"},
        {"balance": "40", "availableBalance": "30"},
        {"balance": "40", "availableBalance": "9.99"},
    ])

    def response(method, **params):
        if method == "futures_account_balance_v3":
            return [{"asset": "USDC", "balance": "1000"}, {"asset": "USDT", **next(balances)}]
        if method == "symbol_configuration":
            return [{"marginType": "ISOLATED", "leverage": 30, "isAutoAddMargin": False, "maxNotionalValue": "7500"}]
        assert method in {"position_information_v3", "current_all_open_orders", "current_all_algo_open_orders"}
        return []

    monkeypatch.setattr(sdk, "request", response)
    assert sdk.prepare("BTCUSDT") == D("4.280864195")
    assert sdk.prepare("ETHUSDT") == D("10")
    with pytest.raises(BinanceError, match="可用 USDT 余额不足"):
        sdk.prepare("ETHUSDT")


def test_binance_signal_does_not_filter_depth_using_the_paper_budget(tmp_path):
    from spot_agent.binance_monitor import BinanceMonitor
    from spot_agent.perpetual_btc import BTC_POLICY
    from spot_agent.perpetual_monitor import SignalJournal
    from spot_agent.wechat_research import signal_message

    instrument = "BTC-USDT-SWAP"
    closed_at = 1795419300000
    now = closed_at + 61_000
    monitor = BinanceMonitor(instrument, BinanceMarket(None, "demo"), None, SignalJournal(tmp_path / "signals.jsonl"),
                             BinanceSettings("demo"), BinanceRules(D(".1"), D(".001"), D(".001"), D("10"), D("100")))
    quote = {"ask": 61500, "bid": 61499.9, "mark": 61500, "last": 61500, "ask_size": 0.002,
             "ticker_at": now, "funding_at": now + 600_000}
    signal, issue = monitor.fixed_candidate(BTC_POLICY, {"closed_at": closed_at, "side": "long", "atr": 100}, quote, now)

    assert issue is None
    assert signal["margin_percent"] == 25
    assert "quantity" not in signal and "margin_budget" not in signal
    published = signal | {"instrument": instrument, "exchange": "Binance", "execution_mode": "demo"}
    assert "钱包余额的 25% × 30 倍" in signal_message(published)


def test_sdk_sends_entry_and_protection_with_distinct_client_ids(monkeypatch):
    import hashlib
    import hmac
    import json
    from urllib.parse import parse_qs, urlparse

    import requests

    calls = []

    def request(self, method, url, **kw):
        parsed = urlparse(url)
        assert parsed.netloc == "demo-fapi.binance.com"
        assert kw["headers"]["X-MBX-APIKEY"] == "test-key"
        payload, signature = kw["params"].rsplit("&signature=", 1)
        assert hmac.new(b"test-secret", payload.encode(), hashlib.sha256).hexdigest() == signature
        args = {k: v[0] for k, v in parse_qs(payload).items()}
        calls.append((method, parsed.path, args))
        res = requests.Response()
        res.status_code = 200
        data = {"status": "EXPIRED", "executedQty": "0.03", "orderId": 42} if parsed.path.endswith("/order") else {"algoStatus": "NEW", "algoId": 43}
        res._content = json.dumps(data).encode()
        return res

    monkeypatch.setattr(requests.Session, "request", request)
    sdk = BinanceFutures(BinanceSettings("demo", "test-key", "test-secret"))
    assert sdk.api is not None
    assert sdk.enter("BTCUSDT", True, D("80000.1"), D("0.09"), "tb-test-entry", 1200)["executedQty"] == "0.03"
    assert sdk.protect("BTCUSDT", True, D("79500.2"), "stop", "tb-test-sl")["algoStatus"] == "NEW"
    (_, entry_path, entry), (_, stop_path, stop) = calls
    assert (entry_path, entry["side"], entry["timeInForce"], entry["newClientOrderId"], entry["recvWindow"]) == (
        "/fapi/v1/order", "BUY", "IOC", "tb-test-entry", "1200")
    assert (stop_path, stop["side"], stop["type"], stop["closePosition"], stop["clientAlgoId"]) == (
        "/fapi/v1/algoOrder", "SELL", "STOP_MARKET", "true", "tb-test-sl")
    assert "quantity" not in stop


def test_trade_recovers_partial_fill_then_cancels_sibling_on_settlement(tmp_path, monkeypatch):
    import sqlite3

    from spot_agent.binance_execution import BinanceTrade, TradeLedger

    now = 1795419280000
    signal = {"id": "BTC:case-1", "instrument": "BTC-USDT-SWAP", "trade_agent_id": "trade-example", "side": "long",
              "entry": 80000.1, "stop_loss": 79000, "take_profit": 82000, "expires_at": now + 10_000,
              "exit_at": now + 60_000, "verified_quote": {"funding_at": now + 100_000}}
    calls, notices = [], []
    algos = {}
    filled = {"orderId": 42, "status": "EXPIRED", "executedQty": ".03", "updateTime": now}
    position = {"positionAmt": ".03", "entryPrice": "79999.9", "liquidationPrice": "78000"}

    class Exchange:
        async def call(self, action, *args):
            calls.append(action)
            if action == "prepare":
                return D("100")
            if action == "enter":
                assert args[:4] == ("BTCUSDT", True, D("80000.1"), D("0.037"))
                raise BinanceError("network", unknown=True)
            if action == "order":
                return filled
            if action == "protect":
                self_path = args[-1]
                algos[self_path] = {"algoStatus": "NEW"}
                return algos[self_path]
            if action == "position":
                return position
            if action == "algo":
                return algos[args[0]]
            if action == "cancel":
                algos[args[0]]["algoStatus"] = "CANCELED"
                return
            if action == "fills":
                one = {"qty": ".03", "time": now, "commission": ".05", "commissionAsset": "USDT"}
                return [one | {"orderId": 42, "side": "BUY", "price": "79999.9", "realizedPnl": "0"},
                        one | {"orderId": 44, "side": "SELL", "price": "81999.9", "realizedPnl": "60"}]
            pytest.fail(f"Unexpected SDK action {action}")

    class Market:
        def now(self):
            return now

        async def quote(self, instrument):
            return {"bid": 79999.9, "ask": 80000, "last": 80000, "mark": 80000,
                    "ask_size": 2, "bid_size": 2, "ticker_at": now}

    monkeypatch.setattr("spot_agent.binance_execution.now_ms", lambda: now)
    rules = BinanceRules(D(".1"), D(".001"), D(".001"), D("10"), D("100"))
    file = tmp_path / "trades.sqlite"

    async def full_trade():
        nonlocal position
        with sqlite3.connect(file) as connection:
            ledger = TradeLedger(connection, "account")
            assert ledger.add(signal)
            worker = BinanceTrade(signal, {}, ledger, Exchange(), Market(), rules, notices.append, "demo")
            with pytest.raises(BinanceError, match="network"):
                await worker.step()
        with sqlite3.connect(file) as connection:
            ledger = TradeLedger(connection, "account")
            assert not ledger.add(signal)
            assert not ledger.add(signal | {"id": "BTC:case-2"})
            _, state = next(ledger.pending())
            worker = BinanceTrade(signal, state, ledger, Exchange(), Market(), rules, notices.append, "demo")
            await worker.step()
            assert calls.count("enter") == 1
            assert len(algos) == 2
            assert ledger.status(signal["id"]) == {"status": "protected", "margin_budget": 100.0}
            assert [n.kind for n in notices] == ["protected"]
            position = None
            algos[worker.protections["target"]] = {"algoStatus": "FINISHED", "actualOrderId": "44"}
            assert not await worker.step()
            assert await worker.step()
            assert algos[worker.protections["stop"]]["algoStatus"] == "CANCELED"
            outcome = ledger.outcome(signal["id"])
            assert outcome["status"] == "binance_closed" and outcome["pnl_usdt"] == 59.90
            assert [(n.kind, n.text.splitlines()[1]) for n in notices] == [("protected", "成功"), ("closed", "成功")]
            assert "止盈触发" in notices[-1].text and not list(ledger.pending())
            assert "保证金预算 100 USDT" in notices[0].text

    asyncio.run(full_trade())


def test_trade_flattens_only_open_size_when_protection_is_rejected(tmp_path):
    import sqlite3

    from spot_agent.binance_execution import BinanceTrade, TradeLedger

    now = 1795419280000
    signal = {"id": "ETH:case-1", "instrument": "ETH-USDT-SWAP", "trade_agent_id": "trade-eth", "side": "short",
              "entry": 2000.0, "stop_loss": 2040.0, "take_profit": 1940.0, "expires_at": now + 1000,
              "exit_at": now + 100_000, "verified_quote": {"funding_at": now + 200_000}}
    sent, notices = [], []

    class Exchange:
        async def call(self, action, *args):
            if action == "position":
                return {"positionAmt": "-1.25", "entryPrice": "2000", "liquidationPrice": "2060"}
            if action == "protect":
                assert args[3] == "stop"
                raise BinanceError("would trigger immediately", code=-2021)
            assert action == "close"
            sent.append(args)

    async def scenario():
        with sqlite3.connect(tmp_path / "trades.sqlite") as connection:
            ledger = TradeLedger(connection, "account")
            ledger.add(signal)
            order = {"orderId": 11, "status": "EXPIRED", "executedQty": "1.25", "updateTime": now}
            worker = BinanceTrade(signal, {"entry_sent": now, "entry": order}, ledger, Exchange(),
                                  SimpleNamespace(now=lambda: now), None, notices.append, "demo")
            await worker.step()
            assert sent[0][:3] == ("ETHUSDT", False, D("1.25"))
            assert "止损挂单被拒绝" in notices[0].body
            assert ledger.status(signal["id"])["status"] == "closing"

    asyncio.run(scenario())


def test_trade_uses_exchange_clock_for_entry_window_and_ignores_prior_fills_on_settle(tmp_path, monkeypatch):
    import sqlite3

    from spot_agent.binance_execution import BinanceTrade, TradeLedger

    exchange_now = 1795419280000
    # Local clock lags behind Binance by 2 seconds; entry window must still follow exchange time.
    monkeypatch.setattr("spot_agent.binance_execution.now_ms", lambda: exchange_now - 2000)
    signal = {"id": "BTC:clock-case", "instrument": "BTC-USDT-SWAP", "trade_agent_id": "trade-clock", "side": "long",
              "entry": 80000.1, "stop_loss": 79000, "take_profit": 82000, "expires_at": exchange_now + 600,
              "exit_at": exchange_now + 60_000, "verified_quote": {"funding_at": exchange_now + 100_000}}
    rules = BinanceRules(D(".1"), D(".001"), D(".001"), D("10"), D("100"))
    notices = []

    class Exchange:
        async def call(self, action, *args):
            if action == "prepare":
                return D("100")
            if action == "position":
                return None
            if action == "fills":
                return [
                    {"orderId": 10, "side": "SELL", "qty": ".03", "price": "79900", "time": exchange_now - 500,
                     "realizedPnl": "-10", "commission": ".05", "commissionAsset": "USDT"},
                    {"orderId": 42, "side": "BUY", "qty": ".03", "price": "80000", "time": exchange_now,
                     "realizedPnl": "0", "commission": ".05", "commissionAsset": "USDT"},
                    {"orderId": 44, "side": "SELL", "qty": ".03", "price": "81000", "time": exchange_now + 500,
                     "realizedPnl": "30", "commission": ".05", "commissionAsset": "USDT"},
                ]
            pytest.fail(f"Unexpected SDK action {action}")

    class Market:
        def now(self):
            return exchange_now

        async def quote(self, instrument):
            return {"bid": 79999.9, "ask": 80000, "last": 80000, "mark": 80000,
                    "ask_size": 2, "bid_size": 2, "ticker_at": exchange_now}

    async def scenario():
        with sqlite3.connect(tmp_path / "trades.sqlite") as connection:
            ledger = TradeLedger(connection, "account")
            ledger.add(signal)
            worker = BinanceTrade(signal, {}, ledger, Exchange(), Market(), rules, notices.append, "demo")
            assert await worker.step() is True
            assert ledger.outcome(signal["id"])["status"] == "entry_expired"

            settled_signal = signal | {"id": "BTC:settle-case", "expires_at": exchange_now + 10_000}
            ledger.add(settled_signal)
            order = {"orderId": 42, "status": "FILLED", "executedQty": ".03", "updateTime": exchange_now}
            settler = BinanceTrade(settled_signal, {"entry_sent": exchange_now, "entry": order}, ledger,
                                   Exchange(), Market(), rules, notices.append, "demo")
            assert await settler.step() is True
            outcome = ledger.outcome(settled_signal["id"])
            assert outcome["status"] == "binance_closed" and outcome["pnl_usdt"] == 29.9

    asyncio.run(scenario())
