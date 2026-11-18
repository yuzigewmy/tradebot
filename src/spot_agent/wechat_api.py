from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, Field

from .wechat_ilink import ILinkError

router = APIRouter(prefix="/api/wechat", tags=["微信机器人"])


class VerifyCode(BaseModel):
    code: str = Field(pattern=r"^[0-9]{4,8}$")


def error_response(code, message, status):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@router.get("")
async def status(request: Request):
    return request.app.state.wechat.view()


@router.post("/binding")
async def begin(request: Request):
    try:
        return await request.app.state.wechat.begin()
    except ILinkError as error:
        return error_response("WECHAT", str(error), 502)
    except httpx.RequestError:
        return error_response("WECHAT_NETWORK", "微信扫码接口暂时无法连接", 502)


@router.get("/binding/qr")
async def qr_image(request: Request):
    if (image := request.app.state.wechat.qr) is None:
        return error_response("WECHAT_QR", "当前没有可扫描的二维码", 404)
    return Response(image, media_type="image/png", headers={"Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin"})


@router.post("/binding/verification", status_code=204)
async def verify(body: VerifyCode, request: Request):
    try:
        request.app.state.wechat.verify(body.code)
    except ILinkError as error:
        return error_response("WECHAT_VERIFY", str(error), 409)


@router.delete("/binding", status_code=204)
async def unlink(request: Request):
    await request.app.state.wechat.unlink()
