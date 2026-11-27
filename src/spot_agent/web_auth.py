"""Session cookie authentication, pending user registration, and admin RBAC."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
from tempfile import NamedTemporaryFile
from time import monotonic, time, time_ns
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
BEIJING = ZoneInfo("Asia/Shanghai")
COOKIE_NAME = "tb_session"
ROLE_COOKIE_NAME = "tb_role"
SESSION_TTL_SECONDS = 7 * 24 * 3600
TEMP_SESSION_TTL_SECONDS = 12 * 3600
RATE_WINDOW_SECONDS = 60.0
RATE_MAX_FAILURES = 6
REGISTER_WINDOW_SECONDS = 300.0
REGISTER_MAX_ATTEMPTS = 5
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
PENDING_NOTICE = "请耐心等待管理员审核，审核通过后可正常登录"
PUBLIC_PATHS = frozenset({
    "/login",
    "/favicon.ico",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/register",
    "/api/auth/status",
    "/static/login.css",
    "/static/login.js",
    "/static/styles.css",
})
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebAuthConfig:
    username: str
    password: str
    secret: bytes

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)

    @classmethod
    def from_env(cls) -> WebAuthConfig:
        username = os.getenv("WEB_AUTH_USER", "").strip()
        password = os.getenv("WEB_AUTH_PASSWORD", "")
        raw_secret = os.getenv("WEB_AUTH_SECRET", "").strip()
        if raw_secret:
            secret = raw_secret.encode("utf-8")
        elif username and password:
            secret = hashlib.sha256(f"tradebot-web-auth:{username}:{password}".encode("utf-8")).digest()
        else:
            secret = b""
        return cls(username=username, password=password, secret=secret)


@dataclass(frozen=True)
class AuthSession:
    username: str
    role: str  # "admin" | "member"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    remember: bool = True
    next: str | None = Field(default=None, max_length=256)


class RegisterInput(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=200)


class LoginRateLimiter:
    def __init__(self, max_failures: int = RATE_MAX_FAILURES, window_seconds: float = RATE_WINDOW_SECONDS):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.failures: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self.failures.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def retry_after(self, key: str) -> int:
        now = monotonic()
        bucket = self._prune(key, now)
        if len(bucket) < self.max_failures:
            return 0
        return max(1, int(self.window_seconds - (now - bucket[0])) + 1)

    def record_failure(self, key: str) -> None:
        now = monotonic()
        self._prune(key, now).append(now)

    def clear(self, key: str) -> None:
        self.failures.pop(key, None)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${_b64url_encode(salt)}${_b64url_encode(derived)}"


def verify_password(password: str, encoded_hash: str) -> bool:
    parts = encoded_hash.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        salt = _b64url_decode(parts[1])
        expected = _b64url_decode(parts[2])
    except ValueError:
        return False
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=len(expected))
    return hmac.compare_digest(derived, expected)


def format_beijing(ts_ms: int | None) -> str | None:
    if not ts_ms:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, BEIJING).strftime("%Y-%m-%d %H:%M:%S")


class UserStore:
    """Atomic JSON-backed store for registered member accounts."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, records: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as tmp:
            json.dump(records, tmp, ensure_ascii=False, indent=2)
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)

    def get(self, username: str) -> dict | None:
        return self._load().get(username.strip().lower())

    def list_users(self, status: str = "all") -> list[dict]:
        records = list(self._load().values())
        if status != "all":
            records = [r for r in records if r["status"] == status]
        records.sort(key=lambda r: r["created_at"], reverse=True)
        return [self.public_view(r) for r in records]

    def register(self, username: str, password: str, display_name: str, note: str, admin_username: str) -> dict:
        norm = username.strip().lower()
        if not USERNAME_RE.match(norm):
            raise ValueError("账号仅支持 3-32 位字母、数字、下划线、点或短横线。")
        if admin_username and norm == admin_username.strip().lower():
            raise ValueError("该账号为保留管理员账号，请更换其他账号。")
        records = self._load()
        existing = records.get(norm)
        if existing and existing["status"] in ("pending", "approved"):
            raise FileExistsError("该账号已存在或正在审核中，请更换其他账号。")
        record = {
            "username": norm,
            "display_name": display_name.strip(),
            "note": note.strip(),
            "password_hash": hash_password(password),
            "status": "pending",
            "role": "member",
            "created_at": time_ns() // 1_000_000,
            "reviewed_at": None,
            "review_note": "",
        }
        records[norm] = record
        self._save(records)
        return self.public_view(record)

    def review(self, username: str, decision: str, review_note: str = "") -> dict:
        norm = username.strip().lower()
        records = self._load()
        record = records.get(norm)
        if record is None:
            raise KeyError(f"未找到用户 {norm} 的注册记录。")
        if decision not in ("approve", "reject"):
            raise ValueError("审核动作仅支持 approve 或 reject。")
        record["status"] = "approved" if decision == "approve" else "rejected"
        record["reviewed_at"] = time_ns() // 1_000_000
        record["review_note"] = review_note.strip()
        records[norm] = record
        self._save(records)
        return self.public_view(record)

    @staticmethod
    def public_view(record: dict) -> dict:
        return {
            "username": record["username"],
            "display_name": record["display_name"],
            "note": record["note"],
            "status": record["status"],
            "role": record["role"],
            "created_at": record["created_at"],
            "created_at_beijing": format_beijing(record["created_at"]),
            "reviewed_at": record["reviewed_at"],
            "reviewed_at_beijing": format_beijing(record["reviewed_at"]),
            "review_note": record["review_note"],
        }


def default_user_store_path() -> Path:
    custom = os.getenv("WEB_USERS_PATH", "").strip()
    return Path(custom) if custom else ROOT / "data" / "web_users.json"


def get_user_store(request: Request) -> UserStore:
    store = getattr(request.app.state, "user_store", None)
    if store is None:
        store = UserStore(default_user_store_path())
        request.app.state.user_store = store
    return store


_rate_limiter = LoginRateLimiter()
_register_limiter = LoginRateLimiter(max_failures=REGISTER_MAX_ATTEMPTS, window_seconds=REGISTER_WINDOW_SECONDS)
router = APIRouter(tags=["认证"])


def safe_next_path(raw: str | None, default: str = "/account", allow_account: bool = True) -> str:
    if not raw:
        return default
    candidate = raw.strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return default
    if candidate.startswith("/login") or candidate.startswith("/api/"):
        return default
    if not allow_account and is_account_path(candidate):
        return default
    return candidate


def is_account_path(path: str) -> bool:
    clean = path.split("?", 1)[0]
    return clean == "/account" or clean.startswith("/account/") or clean == "/api/account" or clean.startswith("/api/account/")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def create_session_token(config: WebAuthConfig, username: str, ttl_seconds: int, role: str = "admin") -> str:
    payload = json.dumps({"u": username, "r": role, "exp": int(time()) + ttl_seconds}, separators=(",", ":")).encode("utf-8")
    encoded = _b64url_encode(payload)
    sig = hmac.new(config.secret, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64url_encode(sig)}"


def verify_session(config: WebAuthConfig, token: str | None, store: UserStore | None = None) -> AuthSession | None:
    if not config.enabled or not token or "." not in token:
        return None
    encoded, sig = token.split(".", 1)
    expected = _b64url_encode(hmac.new(config.secret, encoded.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    user = payload.get("u")
    role = payload.get("r", "admin")
    exp = payload.get("exp")
    if not isinstance(user, str) or not isinstance(role, str) or not isinstance(exp, int):
        return None
    if exp <= int(time()):
        return None
    if role == "admin":
        if hmac.compare_digest(user, config.username):
            return AuthSession(username=user, role="admin")
        return None
    if role == "member":
        user_store = store or UserStore(default_user_store_path())
        record = user_store.get(user)
        if record is not None and record["status"] == "approved":
            return AuthSession(username=record["username"], role="member")
    return None


def verify_session_token(config: WebAuthConfig, token: str | None, store: UserStore | None = None) -> str | None:
    session = verify_session(config, token, store)
    return session.username if session else None


def authenticated_session(request: Request, config: WebAuthConfig) -> AuthSession | None:
    return verify_session(config, request.cookies.get(COOKIE_NAME), get_user_store(request))


def authenticated_user(request: Request, config: WebAuthConfig) -> str | None:
    session = authenticated_session(request, config)
    return session.username if session else None


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS


def login_redirect_url(request: Request) -> str:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    safe = safe_next_path(target, default="")
    if not safe or safe == "/":
        return "/login"
    return f"/login?next={quote(safe, safe='/?=&')}"


async def _notify_registration(app, record: dict) -> None:
    bridge = getattr(app.state, "wechat", None)
    research = getattr(app.state, "wechat_research", None)
    if bridge is None:
        return
    if research is not None:
        text = await research.compose_registration_notice(record)
    else:
        note_line = f"\n申请说明：{record['note']}" if record["note"] else ""
        text = (
            f"BOSS，有新的研究台注册申请待审核：\n"
            f"账号：{record['username']}\n"
            f"称呼：{record['display_name']}{note_line}\n"
            f"提交时间：{record['created_at_beijing']}（北京时间）\n"
            f"请直接回复「通过 {record['username']}」或「拒绝 {record['username']}」。"
        )
    key = f"reg:{record['username']}:{record['created_at']}"
    bridge.publish_notice(key, "注册审核申请", text)


@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    config = WebAuthConfig.from_env()
    session = authenticated_session(request, config) if config.enabled else None
    if not config.enabled:
        return RedirectResponse(url=safe_next_path(request.query_params.get("next")), status_code=302)
    if session is not None:
        default_home = "/account" if session.is_admin else "/"
        next_path = safe_next_path(request.query_params.get("next"), default=default_home, allow_account=session.is_admin)
        return RedirectResponse(url=next_path, status_code=302)
    return FileResponse(ROOT / "web" / "login.html", headers={"Cache-Control": "no-store"})


@router.get("/api/auth/status")
def auth_status(request: Request):
    config = WebAuthConfig.from_env()
    session = authenticated_session(request, config) if config.enabled else None
    mode = getattr(request.app.state, "binance_mode", "off")
    is_admin = True if not config.enabled else bool(session and session.is_admin)
    return JSONResponse(
        {
            "enabled": config.enabled,
            "authenticated": (session is not None) if config.enabled else True,
            "user": session.username if session else None,
            "role": (session.role if session else None) if config.enabled else "admin",
            "is_admin": is_admin,
            "can_view_account": is_admin,
            "execution_mode": mode,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/auth/register", status_code=201)
async def auth_register(request: Request, body: RegisterInput):
    config = WebAuthConfig.from_env()
    client_key = request.client.host if request.client else "unknown"
    retry_after = _register_limiter.retry_after(client_key)
    if retry_after > 0:
        return JSONResponse(
            {
                "error": {
                    "code": "RATE_LIMITED",
                    "message": f"提交注册过于频繁，请 {retry_after} 秒后再试。",
                    "retry_after": retry_after,
                }
            },
            status_code=429,
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )

    store = get_user_store(request)
    try:
        record = store.register(body.username, body.password, body.display_name, body.note, config.username)
    except ValueError as exc:
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": str(exc)}}, status_code=422)
    except FileExistsError as exc:
        return JSONResponse({"error": {"code": "USER_EXISTS", "message": str(exc)}}, status_code=409)

    _register_limiter.record_failure(client_key)
    task = asyncio.create_task(_notify_registration(request.app, record))
    task.add_done_callback(lambda t: log.warning("注册微信通知异常：%s", t.exception()) if not t.cancelled() and t.exception() else None)
    return JSONResponse(
        {
            "ok": True,
            "status": "pending",
            "username": record["username"],
            "message": PENDING_NOTICE,
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/auth/login")
def auth_login(request: Request, body: LoginInput):
    config = WebAuthConfig.from_env()
    if not config.enabled:
        return JSONResponse({"ok": True, "user": None, "role": "admin", "redirect": safe_next_path(body.next)})

    client_key = request.client.host if request.client else "unknown"
    retry_after = _rate_limiter.retry_after(client_key)
    if retry_after > 0:
        return JSONResponse(
            {
                "error": {
                    "code": "RATE_LIMITED",
                    "message": f"连续尝试次数过多，请 {retry_after} 秒后再试。",
                    "retry_after": retry_after,
                }
            },
            status_code=429,
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )

    submitted_user = body.username.strip()
    role: str | None = None
    canonical_user: str | None = None

    if hmac.compare_digest(submitted_user, config.username) and hmac.compare_digest(body.password, config.password):
        role = "admin"
        canonical_user = config.username
    else:
        record = get_user_store(request).get(submitted_user)
        if record is not None and verify_password(body.password, record["password_hash"]):
            if record["status"] == "pending":
                return JSONResponse(
                    {"error": {"code": "PENDING_APPROVAL", "message": PENDING_NOTICE}},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
            if record["status"] == "rejected":
                return JSONResponse(
                    {"error": {"code": "REJECTED", "message": "您的注册申请未通过管理员审核。"}},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
            role = "member"
            canonical_user = record["username"]

    if role is None or canonical_user is None:
        _rate_limiter.record_failure(client_key)
        return JSONResponse(
            {"error": {"code": "INVALID_CREDENTIALS", "message": "账号或密码不正确，请核对后重试。"}},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    _rate_limiter.clear(client_key)
    is_admin = role == "admin"
    ttl = SESSION_TTL_SECONDS if body.remember else TEMP_SESSION_TTL_SECONDS
    token = create_session_token(config, canonical_user, ttl, role=role)
    default_home = "/account" if is_admin else "/"
    redirect_to = safe_next_path(body.next, default=default_home, allow_account=is_admin)
    response = JSONResponse(
        {"ok": True, "user": canonical_user, "role": role, "can_view_account": is_admin, "redirect": redirect_to},
        headers={"Cache-Control": "no-store"},
    )
    max_age = SESSION_TTL_SECONDS if body.remember else None
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=ROLE_COOKIE_NAME,
        value=role,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, samesite="lax")
    response.delete_cookie(key=ROLE_COOKIE_NAME, path="/", samesite="lax")
    return response
