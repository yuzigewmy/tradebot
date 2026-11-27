from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

Instrument = Literal["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
router = APIRouter(prefix="/api/perpetual", tags=["永续合约"])


class MonitorOptions(BaseModel):
    mode: Literal["observe", "signals"] = "observe"
    strategy: Literal["all", "channel_breakout", "trend_pullback", "eth_compression_short", "btc_trend_reclaim", "dual_trend_engine", "macro_a1_aggressive", "macro_a1_steady", "auto_regime_router"] = "all"


@router.get("/{instrument}")
async def monitor_status(instrument: Instrument, request: Request):
    return request.app.state.perpetual[instrument].view()


@router.post("/{instrument}/start")
async def start_monitor(instrument: Instrument, body: MonitorOptions, request: Request):
    monitor = request.app.state.perpetual[instrument]
    monitor.start(body.mode, body.strategy)
    return monitor.view()


@router.post("/{instrument}/stop")
async def stop_monitor(instrument: Instrument, request: Request):
    monitor = request.app.state.perpetual[instrument]
    await monitor.stop()
    return monitor.view()
