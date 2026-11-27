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
        assert status.json()["enabled"] is False
        assert status.json()["authenticated"] is True
        assert client.get("/api/health").status_code == 200
        assert client.get("/login", follow_redirects=False).status_code == 302


def test_auth_login_session_and_logout(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_USER", "admin-test")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "SecretPass!99")
    monkeypatch.setenv("WEB_AUTH_SECRET", "unit-test-secret-key")
    web_auth._rate_limiter.failures.clear()

    with TestClient(app) as client:
        # Unauthenticated pages redirect to /login; API returns 401
        r_page = client.get("/account", follow_redirects=False)
        assert r_page.status_code == 302
        assert r_page.headers["location"] == "/login?next=/account"

        r_api = client.get("/api/health")
        assert r_api.status_code == 401

        # Login page and assets remain accessible
        r_login_page = client.get("/login")
        assert r_login_page.status_code == 200
        assert "身份验证" in r_login_page.text
        assert client.get("/static/login.css").status_code == 200

        # Invalid password rejected
        bad = client.post("/api/auth/login", json={"username": "admin-test", "password": "wrong"})
        assert bad.status_code == 401

        # Valid login issues HttpOnly cookie and sanitizes external next URLs
        ok = client.post(
            "/api/auth/login",
            json={"username": "admin-test", "password": "SecretPass!99", "remember": True, "next": "https://evil.example/"},
        )
        assert ok.status_code == 200
        assert ok.json() == {"ok": True, "user": "admin-test", "redirect": "/account"}
        assert web_auth.COOKIE_NAME in client.cookies

        # Authenticated access succeeds
        assert client.get("/api/health").status_code == 200
        st = client.get("/api/auth/status").json()
        assert st["enabled"] is True
        assert st["authenticated"] is True
        assert st["user"] == "admin-test"

        # Logout clears session cookie
        out = client.post("/api/auth/logout")
        assert out.status_code == 200
        assert client.get("/api/health").status_code == 401


def test_auth_rate_limit(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_USER", "admin-test")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "SecretPass!99")
    monkeypatch.setenv("WEB_AUTH_SECRET", "unit-test-secret-key")
    web_auth._rate_limiter.failures.clear()

    with TestClient(app) as client:
        for _ in range(web_auth.RATE_MAX_FAILURES):
            res = client.post("/api/auth/login", json={"username": "admin-test", "password": "wrong"})
            assert res.status_code == 401
        blocked = client.post("/api/auth/login", json={"username": "admin-test", "password": "SecretPass!99"})
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    web_auth._rate_limiter.failures.clear()
