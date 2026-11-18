"""Owner-only WeChat binding, message polling and live signal delivery."""

import asyncio
from contextlib import suppress
from collections import deque
import csv
import hashlib
import io
import logging
import os
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile


import httpx
from pydantic import BaseModel, Field, ValidationError
import segno

from .perpetual_market import now_ms
from .wechat_ilink import BASE_URL, ILinkError, api_base, wechat_url

log = logging.getLogger(__name__)
NETWORK_ERRORS = (ILinkError, httpx.RequestError)
DELIVERY_ERRORS = (*NETWORK_ERRORS, TimeoutError)


class Binding(BaseModel):
    base: str
    owner: str = Field(min_length=1, repr=False)
    token: str = Field(min_length=1, repr=False)
    context: str = Field(default="", repr=False)
    cursor: str = Field(default="", repr=False)
    seen: list[str] = Field(default_factory=list)
    expired: bool = False


class BindingStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "nt":
            account = subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"], creationflags=subprocess.CREATE_NO_WINDOW)
            _, sid = next(csv.reader([account.decode("ascii", errors="replace").strip()]))
            subprocess.run(["icacls.exe", str(path.parent), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                           check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            path.parent.chmod(0o700)

    def load(self):
        if self.path.exists():
            try:
                state = Binding.model_validate_json(self.path.read_bytes())
            except ValidationError:
                raise ILinkError("本机微信绑定文件格式有误") from None
            state.base = api_base(state.base)
            return state

    def save(self, state):
        with NamedTemporaryFile(dir=self.path.parent, delete=False) as pending:
            pending.write(state.model_dump_json().encode())
        Path(pending.name).replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)


def client_id(kind, value):
    return f"spot-{kind}-" + hashlib.sha256(value.encode()).hexdigest()[:32]


async def stop_task(task):
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class WechatBridge:
    def __init__(self, ilink, store, answer, signal_text, signal_active):
        self.ilink, self.store, self.answer = ilink, store, answer
        self.signal_text, self.signal_active = signal_text, signal_active
        self.binding = store.load()
        self.poll_task = self.send_task = self.pair_task = None
        self.pairing, self.pair_note, self.qr = "idle", "", None
        self.transport, self.delivery = "offline", None
        self.verify_code = asyncio.Queue(maxsize=1)
        self.signals = asyncio.Queue(maxsize=8)
        self.control = asyncio.Lock()
        self.last_poll = self.last_owner_message = None
        self.conversation = deque(maxlen=3)

    def start(self):
        if self.binding is not None and not self.binding.expired:
            self.transport = "connecting"
            self.poll_task = asyncio.create_task(self.poll(self.binding))
            self.send_task = asyncio.create_task(self.deliver(self.binding))

    async def close(self):
        await stop_task(self.pair_task)
        await self.stop_connection()

    async def stop_connection(self):
        await stop_task(self.poll_task)
        await stop_task(self.send_task)
        self.poll_task = self.send_task = None
        self.signals = asyncio.Queue(maxsize=8)

    def view(self):
        state = self.binding
        connection = "unbound" if state is None else "expired" if state.expired else self.transport
        return {
            "connection": connection, "pairing": self.pairing, "pair_note": self.pair_note,
            "qr_available": self.qr is not None, "has_context": bool(state and state.context and not state.expired),
            "last_poll": self.last_poll, "last_owner_message": self.last_owner_message, "last_delivery": self.delivery,
        }

    async def begin(self):
        async with self.control:
            if self.binding is not None and not self.binding.expired:
                return self.view()
            if self.pair_task is not None and not self.pair_task.done():
                return self.view()
            code = await self.ilink.new_code(None)
            buffer = io.BytesIO()
            segno.make(str(wechat_url(code.qrcode_img_content)), micro=False).save(buffer, kind="png", scale=6, border=4)
            self.qr = buffer.getvalue()
            self.pairing, self.pair_note = "waiting", "请用微信扫一扫并在手机上确认"
            self.verify_code = asyncio.Queue(maxsize=1)
            self.pair_task = asyncio.create_task(self.pair(code.qrcode))
            return self.view()

    def verify(self, code):
        if self.pairing != "verification" or self.verify_code.full():
            raise ILinkError("当前没有等待输入的微信验证码")
        self.verify_code.put_nowait(code)

    async def pair(self, code):
        base, verify = BASE_URL, None
        try:
            async with asyncio.timeout(8 * 60):
                while True:
                    status = await self.ilink.code_status(base, code, verify)
                    verify = None
                    if status.status == "confirmed":
                        if not (status.bot_token and status.ilink_bot_id and status.ilink_user_id):
                            raise ILinkError("微信确认结果缺少扫码账号身份，请重新扫码")
                        state = Binding(base=api_base(status.baseurl or base), owner=status.ilink_user_id, token=status.bot_token)
                        await self.stop_connection()
                        self.store.save(state)
                        self.binding = state
                        self.pairing, self.pair_note = "confirmed", "已绑定，请在微信里给机器人发一条私聊"
                        self.start()
                        return
                    if status.status in {"expired", "verify_code_blocked", "binded_redirect"}:
                        raise ILinkError("本次扫码已结束，请生成新二维码")
                    if status.status == "scaned_but_redirect":
                        base = api_base(f"https://{status.redirect_host}")
                    if status.status == "need_verifycode":
                        self.pairing, self.pair_note = "verification", "请填写微信提示的验证码"
                        verify = await self.verify_code.get()
                        self.pairing, self.pair_note = "scanned", "正在验证微信验证码"
                    elif status.status != "wait":
                        self.pairing, self.pair_note = "scanned", "已扫码，请在微信确认"
                    else:
                        await asyncio.sleep(0.6)
        except TimeoutError:
            self.pairing, self.pair_note = "ended", "扫码等待已超时，请生成新二维码"
        except NETWORK_ERRORS as error:
            self.pairing = "ended"
            self.pair_note = str(error) if isinstance(error, ILinkError) else "微信扫码接口连接失败，请重新生成二维码"
        finally:
            self.qr = None

    async def unlink(self):
        async with self.control:
            await self.close()
            self.store.clear()
            self.binding = None
            self.pairing, self.pair_note, self.qr = "idle", "", None
            self.transport, self.delivery = "offline", None
            self.last_owner_message = self.last_poll = None
            self.conversation.clear()

    def expire(self, state):
        state.expired = True
        self.store.save(state)

    async def poll(self, state):
        timeout = 40
        while not state.expired:
            try:
                updates = await self.ilink.updates(state.base, state.token, state.cursor, timeout)
                self.transport, self.last_poll = "online", now_ms()
                if updates.longpolling_timeout_ms:
                    timeout = max(10, min(80, updates.longpolling_timeout_ms / 1000 + 5))
                for message in sorted(updates.msgs, key=lambda msg: (msg.create_time_ms, msg.seq or 0)):
                    await self.reply(state, message)
                if updates.get_updates_buf and updates.get_updates_buf != state.cursor:
                    state.cursor = updates.get_updates_buf
                    self.store.save(state)
                if not updates.msgs:
                    await asyncio.sleep(0.4)
            except NETWORK_ERRORS as error:
                if isinstance(error, ILinkError) and error.expired:
                    self.expire(state)
                    return
                if self.transport != "reconnecting":
                    log.warning("微信轮询中断（%s），等待重连", type(error).__name__)
                self.transport = "reconnecting"
                await asyncio.sleep(3)

    async def reply(self, state, message):
        key = message.delivery_id
        if (message.from_user_id != state.owner or message.group_id or message.message_type not in (None, 1)
                or not message.context_token or key is None or key in state.seen):
            return
        state.context = message.context_token
        self.last_owner_message = now_ms()
        self.store.save(state)
        text = await self.answer(message.text, self.conversation)
        await self.ilink.send_text(state.base, state.token, state.owner, state.context, client_id("reply", key), text)
        state.seen = [*state.seen[-127:], key]
        self.store.save(state)

    def publish(self, signal):
        if self.binding is None or self.binding.expired or not self.binding.context:
            return
        if self.signals.full():
            self.signals.get_nowait()
        self.signals.put_nowait(signal)

    async def deliver(self, state):
        while not state.expired:
            signal = await self.signals.get()
            if state.expired or not self.signal_active(signal):
                continue
            deadline = (signal["expires_at"] - now_ms()) / 1000
            if deadline <= 0:
                continue
            try:
                async with asyncio.timeout(min(deadline, 10)):
                    await self.ilink.send_text(state.base, state.token, state.owner, state.context,
                                               client_id("signal", signal["id"]), self.signal_text(signal))
                self.delivery = {"at": now_ms(), "status": "sent", "instrument": signal["instrument"]}
            except DELIVERY_ERRORS as error:
                if isinstance(error, ILinkError) and error.expired:
                    self.expire(state)
                self.delivery = {"at": now_ms(), "status": "failed", "instrument": signal["instrument"]}
                log.warning("微信信号 %s 投递失败（%s）", signal["id"], type(error).__name__)
