from datetime import datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spot_agent.account_api import BEIJING, analyze_income, income_window, router, snapshot


def test_wallet_budget_and_hedged_exposure_use_different_account_fields():
    asset = {"asset": "USDT", "walletBalance": "100", "marginBalance": "102", "availableBalance": "20",
             "unrealizedProfit": "2", "positionInitialMargin": "12", "openOrderInitialMargin": "3", "maintMargin": "1"}
    position = {"marginAsset": "USDT", "entryPrice": "100", "markPrice": "100", "liquidationPrice": "80",
                "unRealizedProfit": "0", "positionInitialMargin": "3"}
    rows = [position | {"symbol": "BTCUSDT", "positionAmt": "1", "notional": "100"},
            position | {"symbol": "ETHUSDT", "positionAmt": "-.6", "notional": "-60", "liquidationPrice": "110"}]
    data = snapshot({"assets": [asset]}, rows)
    assert data["order_plan"]["margin"] == 25
    assert data["order_plan"]["notional"] == 750
    assert not data["order_plan"]["available_covers_margin"]
    assert data["risk"]["gross_notional"] == 160
    assert data["risk"]["net_notional"] == 40
    assert data["risk"]["shock_up"] == Decimal("0.4")
    assert data["positions"][1]["liquidation_distance_pct"] == 10


def test_income_keeps_transfers_separate_and_uses_beijing_dates():
    stamp = int(datetime(2026, 11, 25, 0, 30, tzinfo=BEIJING).timestamp() * 1000)
    sample = {"time": stamp, "asset": "USDT"}
    data = analyze_income([sample | {"incomeType": "TRANSFER", "income": "22"},
                           sample | {"incomeType": "COMMISSION", "income": "-0.15"},
                           sample | {"incomeType": "REALIZED_PNL", "income": "2"},
                           sample | {"incomeType": "COMMISSION", "income": "-1", "asset": "BNB"}], income_window(stamp))
    assert data["daily"][0]["date"] == "2026-11-19"
    assert data["daily"][-1]["date"] == "2026-11-25"
    assert data["totals"]["transfer"] == 22
    assert data["totals"]["realized"] + data["totals"]["commission"] == Decimal("1.85")


def test_account_data_requires_local_same_origin_and_cannot_be_cached():
    class Reader:
        async def read(self, section):
            assert section == "snapshot"
            return {"wallet": Decimal("1.00000001")}

    app = FastAPI()
    app.include_router(router)
    app.state.account_reader = Reader()
    local = TestClient(app, client=("127.0.0.1", 50000))
    remote = TestClient(app, client=("192.168.1.10", 50000))
    headers = {"Sec-Fetch-Site": "same-origin"}
    assert local.get("/api/account").status_code == 403
    assert local.get("/api/account/income", headers=headers | {"X-Forwarded-For": "192.168.1.10"}).status_code == 403
    assert remote.get("/api/account", headers=headers).status_code == 403
    response = local.get("/api/account", headers=headers)
    assert response.json() == {"wallet": "1.00000001"}
    assert response.headers["cache-control"] == "no-store"
