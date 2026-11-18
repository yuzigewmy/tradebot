"""Text and QR endpoints from Tencent's iLink bot protocol."""

from base64 import b64encode
import secrets
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from . import __version__

BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.10"
CLIENT_VERSION = str(2 * 65536 + 4 * 256 + 10)


class ILinkError(RuntimeError):
    def __init__(self, message, *, expired=False):
        super().__init__(message)
        self.expired = expired


def wechat_url(value):
    url = httpx.URL(value)
    trusted_host = url.host == "weixin.qq.com" or url.host.endswith(".weixin.qq.com")
    if url.scheme != "https" or not trusted_host or url.port not in (None, 443) or url.userinfo:
        raise ILinkError("微信返回了无效的连接地址")
    return url


def api_base(value):
    url = wechat_url(value)
    if url.query or url.fragment:
        raise ILinkError("微信接口地址包含额外参数")
    return str(url).rstrip("/")


class Reply(BaseModel):
    ret: int = 0
    errcode: int = 0


class LoginCode(Reply):
    qrcode: str = Field(min_length=1, repr=False)
    qrcode_img_content: str = Field(min_length=1, max_length=2000, repr=False)


class LoginStatus(Reply):
    status: Literal["wait", "scaned", "scaned_but_redirect", "need_verifycode", "confirmed",
                    "expired", "verify_code_blocked", "binded_redirect"]
    bot_token: str | None = Field(default=None, repr=False)
    ilink_bot_id: str | None = Field(default=None, repr=False)
    ilink_user_id: str | None = Field(default=None, repr=False)
    baseurl: str | None = None
    redirect_host: str | None = None


class TextItem(BaseModel):
    text: str = ""


class MessageItem(BaseModel):
    type: int
    text_item: TextItem | None = None


class Message(BaseModel):
    message_id: int | None = None
    seq: int | None = None
    message_type: int | None = None
    from_user_id: str = ""
    group_id: str | None = None
    create_time_ms: int = 0
    context_token: str | None = Field(default=None, repr=False)
    item_list: list[MessageItem] = Field(default_factory=list)

    @property
    def delivery_id(self):
        if self.message_id is not None:
            return f"message:{self.message_id}"
        if self.seq is not None:
            return f"seq:{self.seq}"

    @property
    def text(self):
        return "\n".join(item.text_item.text for item in self.item_list if item.type == 1 and item.text_item).strip()


class Updates(Reply):
    msgs: list[Message] = Field(default_factory=list)
    get_updates_buf: str = Field(default="", repr=False)
    longpolling_timeout_ms: int | None = None


class ILink:
    def __init__(self, client):
        self.client = client

    async def _request(self, schema, base, endpoint, *, body=None, params=None, token=None, timeout=12):
        headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": CLIENT_VERSION}
        url = f"{api_base(base)}/ilink/bot/{endpoint}"
        if body is None:
            response = await self.client.get(url, headers=headers, params=params, timeout=timeout, follow_redirects=False)
        else:
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["X-WECHAT-UIN"] = b64encode(str(secrets.randbits(32)).encode()).decode()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            response = await self.client.post(url, headers=headers, params=params, json=body, timeout=timeout, follow_redirects=False)
        if not response.is_success:
            raise ILinkError(f"微信接口返回 HTTP {response.status_code}", expired=bool(token and response.status_code == 401))
        try:
            reply = Reply.model_validate_json(response.content)
            code = reply.errcode or reply.ret
            if code:
                raise ILinkError(f"微信接口返回 {code}", expired=code == -14)
            return schema.model_validate_json(response.content)
        except ValidationError:
            raise ILinkError("微信接口响应格式有误") from None

    @staticmethod
    def _body(**fields):
        return fields | {"base_info": {"channel_version": CHANNEL_VERSION, "bot_agent": f"CryptoResearch/{__version__}"}}

    async def new_code(self, previous_token):
        body = {"local_token_list": [previous_token] if previous_token else []}
        return await self._request(LoginCode, BASE_URL, "get_bot_qrcode", body=body, params={"bot_type": "3"})

    async def code_status(self, base, code, verify):
        params = {"qrcode": code}
        if verify:
            params["verify_code"] = verify
        return await self._request(LoginStatus, base, "get_qrcode_status", params=params, timeout=42)

    async def updates(self, base, token, cursor, timeout):
        return await self._request(Updates, base, "getupdates", token=token,
                                   body=self._body(get_updates_buf=cursor), timeout=timeout)

    async def send_text(self, base, token, owner, context, client_id, text, timeout=12):
        msg = {"from_user_id": "", "to_user_id": owner, "client_id": client_id, "message_type": 2,
               "message_state": 2, "context_token": context, "item_list": [{"type": 1, "text_item": {"text": text}}]}
        await self._request(Reply, base, "sendmessage", token=token, body=self._body(msg=msg), timeout=timeout)
