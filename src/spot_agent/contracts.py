from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TIMEFRAMES = {"1m": 60000, "15m": 900000, "30m": 1800000, "1h": 3600000, "4h": 14400000, "1d": 86400000}
SPOT_TIMEFRAMES = ("1h", "4h", "1d")
PERPETUAL_TIMEFRAMES = ("1m", "15m", "1h")
PROJECT_SCHEMA = "spot-analysis/v1"


class PortfolioInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    quantity: float = Field(ge=0)
    available_quantity: float = Field(ge=0)
    average_cost: float | None = Field(default=None, gt=0)
    cash: float = Field(ge=0)
    risk_percent: float = Field(gt=0, le=10, default=1)
    max_weight_percent: float = Field(gt=0, le=100, default=30)
    exit_price: float = Field(gt=0)
    fee_percent: float = Field(ge=0, le=2, default=0.1)
    slippage_percent: float = Field(ge=0, le=5, default=0.1)

    @model_validator(mode="after")
    def available_is_owned(self):
        if self.available_quantity > self.quantity:
            raise ValueError("可用数量不能超过持币数量")
        return self


class AnalyzeInput(BaseModel):
    source: Literal["live", "synthetic"] = "live"
    exchange: Literal["okx", "binance"] = "okx"
    symbol: Literal["BTC/USDT", "ETH/USDT"] = "BTC/USDT"
    portfolio: PortfolioInput | None = None


class ImportInput(BaseModel):
    csv: str = Field(min_length=1, max_length=2000000)
    exchange: str = Field(default="csv", pattern=r"^[a-zA-Z0-9_-]{1,20}$")
    symbol: Literal["BTC/USDT", "ETH/USDT"] = "BTC/USDT"
    timeframe: Literal["1h", "4h", "1d"] = "1h"
    portfolio: PortfolioInput | None = None


class InsightInput(BaseModel):
    question: str = Field(default="解释多周期矛盾，给出观察条件及反证。", min_length=1, max_length=1200)
