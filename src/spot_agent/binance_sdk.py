"""The official Binance SDK and the application's signed order boundary."""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
import os
from typing import Literal

from binance_common.configuration import ConfigurationRestAPI
from binance_common.errors import (BadRequestError, ClientError, Error, NetworkError, RateLimitBanError,
                                   ServerError, TooManyRequestsError)
from binance_sdk_derivatives_trading_usds_futures.derivatives_trading_usds_futures import DerivativesTradingUsdsFutures
from binance_sdk_derivatives_trading_usds_futures.rest_api.models import enums
from pydantic import TypeAdapter
from pydantic_core import ValidationError

from .binance_market import DEMO_URL, LIVE_URL
from .perpetual_regime_terms import LEVERAGE

MARGIN_PERCENT = 25


@dataclass(frozen=True)
class BinanceSettings:
    mode: Literal["off", "quotes", "demo", "live"]
    key: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)

    @property
    def trading(self):
        return self.mode in ("demo", "live")

    @classmethod
    def from_env(cls):
        try:
            mode = TypeAdapter(Literal["off", "quotes", "demo", "live"]).validate_python(os.getenv("BINANCE_FUTURES_MODE", "off"))
        except ValidationError:
            raise ValueError("BINANCE_FUTURES_MODE 只能为 off、quotes、demo 或 live") from None
        settings = cls(mode, os.getenv("BINANCE_FUTURES_API_KEY", ""), os.getenv("BINANCE_FUTURES_API_SECRET", ""))
        if settings.trading and not (settings.key and settings.secret):
            raise ValueError("币安交易模式需要本机环境变量 BINANCE_FUTURES_API_KEY 和 BINANCE_FUTURES_API_SECRET")
        return settings


class BinanceError(RuntimeError):
    def __init__(self, message, *, code=None, unknown=False, retry_after=None):
        super().__init__(message)
        self.code, self.unknown, self.retry_after = code, unknown, retry_after


def amount(value):
    return format(value, "f")


def payload(value):
    if isinstance(value, list):
        return [payload(item) for item in value]
    return value.to_dict() if hasattr(value, "to_dict") else value


class BinanceFutures:
    def __init__(self, settings):
        # Each order is sent once. A lost response is reconciled using its original client ID.
        config = ConfigurationRestAPI(api_key=settings.key, api_secret=settings.secret, retries=0,
                                      timeout=3000, base_path=DEMO_URL if settings.mode == "demo" else LIVE_URL)
        self.api = DerivativesTradingUsdsFutures(config_rest_api=config).rest_api

    def close(self):
        self.api._session.close()

    def request(self, method, /, *, missing=(), **kwargs):
        try:
            return payload(getattr(self.api, method)(**kwargs).data())
        except Error as exc:
            code = getattr(exc, "status_code", None)
            if isinstance(exc, BadRequestError) and code in missing:
                return None
            unknown = isinstance(exc, (ClientError, NetworkError, ServerError)) or code in (-1000, -1001, -1006, -1007)
            retry_after = None
            if isinstance(exc, (TooManyRequestsError, RateLimitBanError)):
                retry_after = exc.retry_after or (60 if isinstance(exc, RateLimitBanError) else 10)
            # SDK network error strings may contain signed URLs.
            raise BinanceError(f"{method}：{type(exc).__name__}" + (f" {code}" if code is not None else ""),
                               code=code, unknown=unknown, retry_after=retry_after) from None

    async def call(self, operation, *args):
        pending = asyncio.create_task(asyncio.to_thread(getattr(self, operation), *args))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Keep the account lock until the SDK's in-flight HTTP request has returned.
            try:
                await pending
            except BinanceError:
                pass
            raise

    def balance(self):
        balances = self.request("futures_account_balance_v3")
        for row in balances:
            if row["asset"] == "USDT":
                return row
        raise BinanceError("币安合约钱包未返回 USDT 资产")

    def account(self):
        account = self.request("futures_account_configuration")
        if not account["canTrade"] or account["dualSidePosition"] or account["multiAssetsMargin"]:
            raise BinanceError("币安合约账户需要开启交易并使用单向持仓、单资产模式")
        alias = self.balance()["accountAlias"]
        if not alias:
            raise BinanceError("币安合约账户缺少账本标识")
        return alias

    def position(self, symbol):
        rows = self.request("position_information_v3", symbol=symbol)
        return next((row for row in rows if Decimal(row["positionAmt"])), None)

    def prepare(self, symbol):
        if (self.position(symbol) or self.request("current_all_open_orders", symbol=symbol)
                or self.request("current_all_algo_open_orders", symbol=symbol)):
            raise BinanceError(f"{symbol} 存在非本执行器管理的仓位或订单")
        balance = self.balance()
        margin_budget = Decimal(balance["balance"]) * MARGIN_PERCENT / 100
        if margin_budget <= 0:
            raise BinanceError("币安 USDT 钱包余额不足")
        if Decimal(balance["availableBalance"]) < margin_budget:
            raise BinanceError(f"可用 USDT 余额不足，无法覆盖 {margin_budget:f} USDT 保证金预算")
        config, = self.request("symbol_configuration", symbol=symbol)
        if config["isAutoAddMargin"]:
            raise BinanceError(f"{symbol} 需在币安关闭自动追加保证金")
        if config["marginType"] != "ISOLATED":
            self.request("change_margin_type", symbol=symbol, margin_type=enums.ChangeMarginTypeMarginTypeEnum.ISOLATED,
                         missing=(-4046,))
        if config["leverage"] != int(LEVERAGE):
            config = self.request("change_initial_leverage", symbol=symbol, leverage=int(LEVERAGE))
        if Decimal(config["maxNotionalValue"]) < margin_budget * LEVERAGE:
            raise BinanceError(f"{symbol} 杠杆档位不足以覆盖计划名义额")
        return margin_budget

    def enter(self, symbol, long, price, quantity, client_id, window):
        return self.request("new_order", symbol=symbol, side=enums.NewOrderSideEnum("BUY" if long else "SELL"),
                            type=enums.NewOrderTypeEnum.LIMIT, time_in_force=enums.NewOrderTimeInForceEnum.IOC,
                            price=amount(price), quantity=amount(quantity), new_client_order_id=client_id,
                            recv_window=window, new_order_resp_type=enums.NewOrderNewOrderRespTypeEnum.RESULT)

    def protect(self, symbol, long, trigger, leg, client_id):
        kind = "STOP_MARKET" if leg == "stop" else "TAKE_PROFIT_MARKET"
        return self.request("new_algo_order", algo_type=enums.NewAlgoOrderAlgoTypeEnum.CONDITIONAL, symbol=symbol,
                            side=enums.NewAlgoOrderSideEnum("SELL" if long else "BUY"), type=enums.NewAlgoOrderTypeEnum(kind),
                            trigger_price=amount(trigger), working_type=enums.NewAlgoOrderWorkingTypeEnum.CONTRACT_PRICE,
                            close_position=enums.NewAlgoOrderClosePositionEnum("true"), client_algo_id=client_id, recv_window=5000)

    def close(self, symbol, long, quantity, client_id):
        return self.request("new_order", symbol=symbol, side=enums.NewOrderSideEnum("SELL" if long else "BUY"),
                            type=enums.NewOrderTypeEnum.MARKET, quantity=amount(quantity),
                            reduce_only=enums.NewOrderReduceOnlyEnum("true"), new_client_order_id=client_id,
                            recv_window=5000, new_order_resp_type=enums.NewOrderNewOrderRespTypeEnum.RESULT)

    def order(self, symbol, client_id):
        return self.request("query_order", symbol=symbol, orig_client_order_id=client_id, missing=(-2013,))

    def algo(self, client_id):
        return self.request("query_algo_order", client_algo_id=client_id, missing=(-2013,))

    def cancel(self, client_id):
        return self.request("cancel_algo_order", client_algo_id=client_id, missing=(-2011, -2013))

    def fills(self, symbol, start, end):
        return self.request("account_trade_list", symbol=symbol, start_time=start, end_time=end, limit=1000)

    def funding(self, symbol, start, end):
        return self.request("get_income_history", symbol=symbol, income_type=enums.GetIncomeHistoryIncomeTypeEnum.FUNDING_FEE,
                            start_time=start, end_time=end)
