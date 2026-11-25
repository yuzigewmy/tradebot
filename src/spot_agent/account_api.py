"""Local, read-only Binance USDⓈ-M account dashboard."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from time import monotonic, time_ns
from zoneinfo import ZoneInfo

from binance_common.errors import Error
from binance_common.utils import send_request
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .binance_sdk import BinanceError, BinanceFutures, MARGIN_PERCENT
from .perpetual_regime_terms import LEVERAGE

router = APIRouter(prefix="/api/account", tags=["账户"])
BEIJING = ZoneInfo("Asia/Shanghai")
INCOME_LIMIT = 1000
INCOME_GROUPS = {"REALIZED_PNL": "realized", "COMMISSION": "commission", "FUNDING_FEE": "funding",
                 "TRANSFER": "transfer", "INTERNAL_TRANSFER": "transfer", "CROSS_COLLATERAL_TRANSFER": "transfer"}
NO_STORE = {"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"}


def fields(row, aliases):
    return {name: Decimal(row[key]) for name, key in aliases.items()}


def snapshot(account, risks):
    asset_fields = {"wallet": "walletBalance", "equity": "marginBalance", "available": "availableBalance",
                    "unrealized": "unrealizedProfit", "position_margin": "positionInitialMargin",
                    "order_margin": "openOrderInitialMargin", "maintenance_margin": "maintMargin"}
    assets = [{"asset": row["asset"], **fields(row, asset_fields)} for row in account["assets"]]
    wallet = next(row for row in assets if row["asset"] == "USDT")
    position_fields = {"quantity": "positionAmt", "entry": "entryPrice", "mark": "markPrice",
                       "liquidation": "liquidationPrice", "notional": "notional", "unrealized": "unRealizedProfit",
                       "initial_margin": "positionInitialMargin"}
    positions = []
    for row in risks:
        values = fields(row, position_fields)
        if not values["quantity"]:
            continue
        side = 1 if values["quantity"] > 0 else -1
        distance = (side * (values["mark"] - values["liquidation"]) / values["mark"] * 100
                    if values["liquidation"] > 0 and values["mark"] > 0 else None)
        positions.append({"symbol": row["symbol"], "asset": row["marginAsset"], "side": "long" if side == 1 else "short",
                          **values, "liquidation_distance_pct": distance})
    positions.sort(key=lambda row: abs(row["notional"]), reverse=True)
    usdt_positions = [row for row in positions if row["asset"] == "USDT"]
    gross = sum(abs(row["notional"]) for row in usdt_positions)
    net = sum((1 if row["side"] == "long" else -1) * abs(row["notional"]) for row in usdt_positions)
    equity = wallet["equity"]
    used = wallet["position_margin"] + wallet["order_margin"]
    budget = max(wallet["wallet"], Decimal(0)) * MARGIN_PERCENT / 100
    return {"wallet": wallet, "assets": [row for row in assets if row["asset"] == "USDT" or row["wallet"] or row["equity"]],
            "positions": positions, "risk": {"gross_notional": gross, "net_notional": net,
                "margin_usage_pct": used / equity * 100 if equity > 0 else None,
                "gross_leverage": gross / equity if equity > 0 else None,
                "shock_up": net / Decimal(100)},
            "order_plan": {"margin_pct": MARGIN_PERCENT, "leverage": LEVERAGE, "margin": budget,
                           "notional": budget * LEVERAGE, "available_covers_margin": budget > 0 and wallet["available"] >= budget}}


def income_window(now):
    date = datetime.fromtimestamp(now / 1000, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return [date - timedelta(days=offset) for offset in range(6, -1, -1)]


def analyze_income(rows, days):
    groups = ("realized", "commission", "funding", "transfer", "other")
    daily = {day.date().isoformat(): dict.fromkeys(groups, Decimal(0)) for day in days}
    for row in rows:
        if row["asset"] == "USDT":
            date = datetime.fromtimestamp(row["time"] / 1000, BEIJING).date().isoformat()
            daily[date][INCOME_GROUPS.get(row["incomeType"], "other")] += Decimal(row["income"])
    totals = {key: sum(item[key] for item in daily.values()) for key in groups}
    return {"totals": totals, "daily": [{"date": date, **values} for date, values in daily.items()],
            "incomplete": len(rows) == INCOME_LIMIT}


class AccountReader:
    def __init__(self, settings):
        self.settings = settings
        self.cache = {}
        self.locks = {key: asyncio.Lock() for key in ("snapshot", "income")}

    async def read(self, section):
        ttl = 5 if section == "snapshot" else 60
        async with self.locks[section]:
            if section in self.cache and monotonic() - self.cache[section][0] < ttl:
                return self.cache[section][1]
            result = await asyncio.to_thread(self.fetch, section)
            self.cache[section] = (monotonic(), result)
            return result

    def fetch(self, section):
        sdk = BinanceFutures(self.settings)
        with sdk.api._session as session:
            session.max_redirects = 0
            server_time = sdk.request("check_server_time")["serverTime"]
            synced = monotonic()

            def signed(path, **parameters):
                timestamp = server_time + int((monotonic() - synced) * 1000)
                payload = {"timestamp": timestamp, "recvWindow": 5000, **parameters}
                return send_request(session, sdk.api.configuration, "GET", path, payload, is_signed=True).data()

            if section == "snapshot":
                data = snapshot(signed("/fapi/v3/account"), signed("/fapi/v3/positionRisk"))
            else:
                days = income_window(server_time)
                rows = signed("/fapi/v1/income", startTime=int(days[0].timestamp() * 1000), endTime=server_time, limit=INCOME_LIMIT)
                data = analyze_income(rows, days)
        return {"mode": self.settings.mode, "as_of": time_ns() // 1_000_000, **data}


async def read_section(request, section):
    forwarded = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
    if (request.client.host not in ("127.0.0.1", "::1") or request.headers.get("sec-fetch-site") != "same-origin"
            or any(name in request.headers for name in forwarded)):
        return JSONResponse({"error": {"message": "请在本机研究台打开账户页。"}}, status_code=403, headers=NO_STORE)
    reader = request.app.state.account_reader
    if reader is None:
        return JSONResponse({"error": {"message": "当前运行模式未连接币安合约钱包。"}}, status_code=409, headers=NO_STORE)
    try:
        # Keep decimals exact on the wire. The browser rounds only for display.
        data = jsonable_encoder(await reader.read(section), custom_encoder={Decimal: str})
        return JSONResponse(data, headers=NO_STORE)
    except (Error, BinanceError) as exc:
        code = getattr(exc, "status_code", getattr(exc, "code", None))
        message = f"币安账户暂时无法读取（{code}）。" if isinstance(code, int) else "币安账户暂时无法读取。"
        return JSONResponse({"error": {"message": message}}, status_code=502, headers=NO_STORE)


@router.get("")
async def account(request: Request):
    return await read_section(request, "snapshot")


@router.get("/income")
async def income(request: Request):
    return await read_section(request, "income")
