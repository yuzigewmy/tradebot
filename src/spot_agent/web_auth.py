"""Session cookie authentication for public Web console deployments."""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
from time import monotonic, time
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
COOKIE_NAME = "tb_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
TEMP_SESSION_TTL_SECONDS = 12 * 3600
RATE_WINDOW_SECONDS = 60.0
RATE_MAX_FAILURES = 6
PUBLIC_PATHS = frozenset({
    "/login",
    "/favicon.ico",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/status",
    "/static/login.css",
    "/static/login.js",
    "/static/styles.css",
})


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


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    remember: bool = True
    next: str | None = Field(default=None, max_length=256)


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


_rate_limiter = LoginRateLimiter()
router = APIRouter(tags=["认证"])


def safe_next_path(raw: str | None, default: str = "/account") -> str:
    if not raw:
        return default
    candidate = raw.strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return default
    if candidate.startswith("/login") or candidate.startswith("/api/"):
        return default
    return candidate


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def create_session_token(config: WebAuthConfig, username: str, ttl_seconds: int) -> str:
    payload = json.dumps({"u": username, "exp": int(time()) + ttl_seconds}, separators=(",", ":")).encode("utf-8")
    encoded = _b64url_encode(payload)
    sig = hmac.new(config.secret, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64url_encode(sig)}"


def verify_session_token(config: WebAuthConfig, token: str | None) -> str | None:
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
    exp = payload.get("exp")
    if not isinstance(user, str) or not isinstance(exp, int):
        return None
    if exp <= int(time()) or not hmac.compare_digest(user, config.username):
        return None
    return user


def authenticated_user(request: Request, config: WebAuthConfig) -> str | None:
    return verify_session_token(config, request.cookies.get(COOKIE_NAME))


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


@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    config = WebAuthConfig.from_env()
    next_path = safe_next_path(request.query_params.get("next"))
    if not config.enabled or authenticated_user(request, config):
        return RedirectResponse(url=next_path, status_code=302)
    return FileResponse(ROOT / "web" / "login.html", headers={"Cache-Control": "no-store"})


@router.get("/api/auth/status")
def auth_status(request: Request):
    config = WebAuthConfig.from_env()
    user = authenticated_user(request, config) if config.enabled else None
    mode = getattr(request.app.state, "binance_mode", "off")
    return JSONResponse(
        {
            "enabled": config.enabled,
            "authenticated": (user is not None) if config.enabled else True,
            "user": user,
            "execution_mode": mode,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/auth/login")
def auth_login(request: Request, body: LoginInput):
    config = WebAuthConfig.from_env()
    if not config.enabled:
        return JSONResponse({"ok": True, "user": None, "redirect": safe_next_path(body.next)})

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

    valid_user = hmac.compare_digest(body.username.strip(), config.username)
    valid_pass = hmac.compare_digest(body.password, config.password)
    if not (valid_user and valid_pass):
        _rate_limiter.record_failure(client_key)
        return JSONResponse(
            {"error": {"code": "INVALID_CREDENTIALS", "message": "账号或密码不正确，请核对后重试。"}},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    _rate_limiter.clear(client_key)
    ttl = SESSION_TTL_SECONDS if body.remember else TEMP_SESSION_TTL_SECONDS
    token = create_session_token(config, config.username, ttl)
    response = JSONResponse(
        {"ok": True, "user": config.username, "redirect": safe_next_path(body.next)},
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS if body.remember else None,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, samesite="lax")
    return response
