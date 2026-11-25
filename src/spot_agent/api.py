import csv
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
from typing import Annotated

import ccxt
from fastapi import FastAPI, Path as PathParameter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import httpx
import plotly
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .account_api import AccountReader, router as account_router
from .agent import AgentError, model_configured, run_agent
from .binance_monitor import binance_monitors
from .binance_sdk import BinanceSettings
from .contracts import AnalyzeInput, ImportInput, InsightInput, PortfolioInput
from .market import MarketFrame
from .perpetual_api import INSTRUMENTS, router as perpetual_router
from .perpetual_monitor import live_monitors
from .service import ROOT, analyze, assemble, import_analysis, load_report
from .wechat_api import router as wechat_router
from .wechat_bridge import BindingStore, WechatBridge
from .wechat_ilink import ILink
from .wechat_research import WechatResearch, signal_message

@asynccontextmanager
async def lifespan(app):
    binance = BinanceSettings.from_env()

    def forward_signal(signal):
        bridge.publish(signal)

    def forward_trade(notice):
        bridge.publish_trade(notice)

    if binance.mode == "off":
        runtime = live_monitors(ROOT / "data" / "perpetual-signals.jsonl", INSTRUMENTS, forward_signal, on_trade=forward_trade)
    else:
        runtime = binance_monitors(ROOT / "data", binance, forward_signal, forward_trade)
    async with httpx.AsyncClient() as wechat_client, runtime as monitors:
        research = WechatResearch(monitors, monitors[INSTRUMENTS[0]].model_client)
        bridge = WechatBridge(ILink(wechat_client), BindingStore(ROOT / "data" / "wechat" / "binding.json"),
                              research.answer, signal_message, research.active)
        app.state.perpetual, app.state.wechat = monitors, bridge
        app.state.binance_mode = binance.mode
        app.state.account_reader = AccountReader(binance) if binance.trading else None
        bridge.start()
        try:
            if binance.mode != "off":
                for monitor in monitors.values():
                    monitor.start("signals" if binance.trading else "observe")
            yield
        finally:
            await bridge.close()


app = FastAPI(title="加密研究台 API", version=__version__, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.include_router(perpetual_router)
app.include_router(wechat_router)
app.include_router(account_router)
SnapshotId = Annotated[str, PathParameter(pattern=r"^[a-f0-9]{24}$")]
logger = logging.getLogger("spot_agent")


@app.middleware("http")
async def local_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method in {"POST", "DELETE"} and origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse({"error": {"code": "ORIGIN", "message": "仅接受本地页面同源请求"}}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def error_response(code, message, status):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@app.exception_handler(RequestValidationError)
async def input_error(request, error):
    detail = error.errors()[0]
    return error_response("INPUT", ".".join(map(str, detail["loc"])) + ": " + detail["msg"], 422)


@app.exception_handler(ValueError)
@app.exception_handler(csv.Error)
async def data_error(request, error):
    return error_response("DATA", str(error), 422)


@app.exception_handler(FileNotFoundError)
async def report_not_found(request, error):
    return error_response("NOT_FOUND", "未找到该报告快照", 404)


@app.exception_handler(ccxt.BaseError)
async def market_error(request, error):
    logger.warning("public market error: %s", type(error).__name__)
    return error_response("MARKET", "交易所公共行情请求失败（" + type(error).__name__ + "）。请检查网络或所选交易所；未切换到演示数据。", 502)


@app.exception_handler(AgentError)
async def agent_error(request, error):
    return error_response("AGENT", str(error), 409)


@app.exception_handler(httpx.HTTPStatusError)
async def model_http_error(request, error):
    return error_response("MODEL_HTTP", f"模型接口返回HTTP {error.response.status_code}，请检查本机配置与额度。", 502)


@app.exception_handler(httpx.RequestError)
async def model_network_error(request, error):
    return error_response("MODEL_NETWORK", "模型接口连接失败或超时，基础分析仍保留。", 502)


@app.get("/api/health")
def health(request: Request):
    mode = request.app.state.binance_mode
    return {"status": "ok", "version": __version__, "model_configured": model_configured(),
            "execution_mode": mode, "trading_enabled": mode in ("demo", "live")}


@app.post("/api/analyze")
def analyze_market(body: AnalyzeInput):
    return analyze(body)


@app.post("/api/import")
def import_csv(body: ImportInput):
    return import_analysis(body)


@app.get("/api/reports/{snapshot_id}")
def read_report(snapshot_id: SnapshotId):
    return load_report(snapshot_id)


@app.post("/api/reports/{snapshot_id}/portfolio")
def reprice_portfolio(snapshot_id: SnapshotId, body: PortfolioInput):
    saved = load_report(snapshot_id)
    return assemble({tf: MarketFrame(**frame) for tf, frame in saved["raw_frames"].items()}, body)


@app.get("/api/reports/{snapshot_id}/markdown")
def export_report(snapshot_id: SnapshotId):
    report = load_report(snapshot_id)
    return PlainTextResponse(report["report"], media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="spot-report-{snapshot_id}.md"'})


@app.post("/api/reports/{snapshot_id}/insight")
def insight(snapshot_id: SnapshotId, body: InsightInput):
    try:
        result = run_agent(load_report(snapshot_id), body.question)
    except (ValidationError, json.JSONDecodeError) as error:
        raise AgentError("模型返回格式不符合研判契约，结果未采用。") from error
    return result


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/perpetual", include_in_schema=False)
def perpetual_page():
    return FileResponse(ROOT / "web" / "perpetual.html")


@app.get("/account", include_in_schema=False)
def account_page():
    return FileResponse(ROOT / "web" / "account.html")


@app.get("/wechat", include_in_schema=False)
def wechat_page():
    return FileResponse(ROOT / "web" / "wechat.html")


@app.get("/static/plotly.min.js")
def plotly_script():
    return FileResponse(Path(plotly.__file__).parent / "package_data" / "plotly.min.js", media_type="application/javascript")


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="web")
