import asyncio
from decimal import Decimal

from spot_agent.binance_market import BinanceMarket, BinanceRules
from spot_agent.binance_sdk import BinanceFutures, BinanceSettings

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


def test_binance_sizes_to_exchange_step_and_min_notional():
    rules = BinanceRules(D("0.1"), D("0.001"), D("0.001"), D("10"), D("100"))
    assert rules.quantity(D("75123.4"), D("7500")) == D("0.099")
    assert rules.quantity(D("75123.4"), D("120")) is None


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
