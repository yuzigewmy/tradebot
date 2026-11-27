import asyncio
import json

from fastapi.testclient import TestClient

from spot_agent import web_auth
from spot_agent.api import app


def test_auth_disabled_by_default(monkeypatch):
    monkeypatch.delenv("WEB_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_AUTH_SECRET", raising=False)
    with TestClient(app) as client:
        status = client.get("/api/auth/status")
        assert status.status_code == 200
        body = status.json()
        assert body["enabled"] is False
        assert body["authenticated"] is True
        assert body["can_view_account"] is True
        assert client.get("/api/health").status_code == 200
        assert client.get("/login", follow_redirects=False).status_code == 302


def test_auth_login_session_and_logout(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_AUTH_USER", "admin-test")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "SecretPass!99")
    monkeypatch.setenv("WEB_AUTH_SECRET", "unit-test-secret-key")
    monkeypatch.setenv("WEB_USERS_PATH", str(tmp_path / "web_users.json"))
    web_auth._rate_limiter.failures.clear()

    with TestClient(app) as client:
        r_page = client.get("/account", follow_redirects=False)
        assert r_page.status_code == 302
        assert r_page.headers["location"] == "/login?next=/account"

        assert client.get("/api/health").status_code == 401

        r_login_page = client.get("/login")
        assert r_login_page.status_code == 200
        assert "身份验证" in r_login_page.text
        assert "申请注册" in r_login_page.text

        bad = client.post("/api/auth/login", json={"username": "admin-test", "password": "wrong"})
        assert bad.status_code == 401

        ok = client.post(
            "/api/auth/login",
            json={"username": "admin-test", "password": "SecretPass!99", "remember": True, "next": "https://evil.example/"},
        )
        assert ok.status_code == 200
        assert ok.json() == {
            "ok": True,
            "user": "admin-test",
            "role": "admin",
            "can_view_account": True,
            "redirect": "/account",
        }
        assert web_auth.COOKIE_NAME in client.cookies
        assert client.cookies.get(web_auth.ROLE_COOKIE_NAME) == "admin"

        assert client.get("/api/health").status_code == 200
        assert client.get("/account").status_code == 200
        st = client.get("/api/auth/status").json()
        assert st["enabled"] is True
        assert st["authenticated"] is True
        assert st["user"] == "admin-test"
        assert st["can_view_account"] is True

        out = client.post("/api/auth/logout")
        assert out.status_code == 200
        assert client.get("/api/health").status_code == 401


def test_registration_wechat_approval_and_account_rbac(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_AUTH_USER", "boss")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "BossPass!88")
    monkeypatch.setenv("WEB_AUTH_SECRET", "unit-test-secret-key")
    monkeypatch.setenv("WEB_USERS_PATH", str(tmp_path / "web_users.json"))
    web_auth._rate_limiter.failures.clear()
    web_auth._register_limiter.failures.clear()

    pushed_notices = []

    with TestClient(app) as client:
        # Spy on WeChat notification delivery without hitting external APIs
        monkeypatch.setattr(
            app.state.wechat,
            "publish_notice",
            lambda key, title, text: pushed_notices.append((key, title, text)),
        )

        # Cannot register reserved admin username
        reserved = client.post(
            "/api/auth/register",
            json={"username": "boss", "password": "UserPass123", "display_name": "冒充者", "note": ""},
        )
        assert reserved.status_code == 422

        # Register normal user -> pending + WeChat notice pushed
        reg = client.post(
            "/api/auth/register",
            json={"username": "alice_01", "password": "UserPass123", "display_name": "小李", "note": "策略观察"},
        )
        assert reg.status_code == 201
        assert reg.json()["status"] == "pending"
        assert reg.json()["message"] == "请耐心等待管理员审核，审核通过后可正常登录"
        assert len(pushed_notices) == 1
        assert "alice_01" in pushed_notices[0][2]
        assert "小李" in pushed_notices[0][2]

        # Pending user cannot log in yet
        pending_login = client.post("/api/auth/login", json={"username": "alice_01", "password": "UserPass123"})
        assert pending_login.status_code == 403
        assert pending_login.json()["error"]["code"] == "PENDING_APPROVAL"
        assert pending_login.json()["error"]["message"] == "请耐心等待管理员审核，审核通过后可正常登录"

        # BOSS lists and approves via WeChat LLM tools
        listed = asyncio.run(
            app.state.wechat_research.execute(
                {"name": "list_registrations", "arguments": json.dumps({"status": "pending"})}
            )
        )
        assert len(listed["users"]) == 1
        assert listed["users"][0]["username"] == "alice_01"

        approved = asyncio.run(
            app.state.wechat_research.execute(
                {
                    "name": "review_registration",
                    "arguments": json.dumps({"username": "alice_01", "decision": "approve", "note": ""}),
                }
            )
        )
        assert approved["ok"] is True
        assert approved["user"]["status"] == "approved"

        # Approved member can log in, redirects to "/" even if next=/account, and CANNOT access /account or /api/account
        member_login = client.post(
            "/api/auth/login",
            json={"username": "alice_01", "password": "UserPass123", "next": "/account"},
        )
        assert member_login.status_code == 200
        assert member_login.json()["role"] == "member"
        assert member_login.json()["can_view_account"] is False
        assert member_login.json()["redirect"] == "/"
        assert client.cookies.get(web_auth.ROLE_COOKIE_NAME) == "member"

        # Member can view main workspace & health, but NOT /account or /api/account
        assert client.get("/").status_code == 200
        assert client.get("/perpetual").status_code == 200
        assert client.get("/api/health").status_code == 200
        st = client.get("/api/auth/status").json()
        assert st["role"] == "member"
        assert st["can_view_account"] is False

        acct_page = client.get("/account", follow_redirects=False)
        assert acct_page.status_code == 302
        assert acct_page.headers["location"] == "/"

        acct_api = client.get("/api/account", headers={"Sec-Fetch-Site": "same-origin"})
        assert acct_api.status_code == 403
        assert acct_api.json()["error"]["code"] == "FORBIDDEN"

        # BOSS revokes member via WeChat -> existing member session is immediately invalidated
        revoked = asyncio.run(
            app.state.wechat_research.execute(
                {
                    "name": "review_registration",
                    "arguments": json.dumps({"username": "alice_01", "decision": "reject", "note": ""}),
                }
            )
        )
        assert revoked["user"]["status"] == "rejected"
        assert client.get("/api/health").status_code == 401


def test_wechat_strict_tool_schemas():
    from spot_agent.wechat_research import TOOLS

    for tool in TOOLS:
        if tool.get("type") == "function" and tool.get("strict"):
            params = tool["parameters"]
            props = set(params.get("properties", {}).keys())
            req = set(params.get("required", []))
            assert params.get("additionalProperties") is False
            assert req == props, f"Tool {tool['name']} strict schema missing required fields: {props - req}"


def test_auth_rate_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_AUTH_USER", "admin-test")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "SecretPass!99")
    monkeypatch.setenv("WEB_AUTH_SECRET", "unit-test-secret-key")
    monkeypatch.setenv("WEB_USERS_PATH", str(tmp_path / "web_users.json"))
    web_auth._rate_limiter.failures.clear()

    with TestClient(app) as client:
        for _ in range(web_auth.RATE_MAX_FAILURES):
            res = client.post("/api/auth/login", json={"username": "admin-test", "password": "wrong"})
            assert res.status_code == 401
        blocked = client.post("/api/auth/login", json={"username": "admin-test", "password": "SecretPass!99"})
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    web_auth._rate_limiter.failures.clear()
