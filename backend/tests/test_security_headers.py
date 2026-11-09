"""NF03 service side: security headers, HTTPS enforcement behind a proxy, and the web client's CSP."""

from __future__ import annotations

from fastapi.testclient import TestClient

from ajgem.__main__ import with_web_client
from ajgem.security import content_security_policy


def test_api_responses_are_hardened(client):
    r = client.get("/auth/config")
    h = r.headers
    assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
    assert h["x-frame-options"] == "SAMEORIGIN" and h["cache-control"] == "no-store"
    assert "strict-transport-security" not in h  # plain HTTP in tests
    r = client.get("/auth/config", headers={"X-Forwarded-Proto": "https"})
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")


def test_https_can_be_required(app, client):
    app.state.ctx.settings.require_https = True
    try:
        r = client.get("/auth/config")
        assert r.status_code == 403 and r.json()["error"]["code"] == "HTTPS_REQUIRED"
        assert client.get("/auth/config", headers={"X-Forwarded-Proto": "https"}).status_code == 200
        assert client.get("/health").status_code in (200, 503)  # probes still reach the health check
    finally:
        app.state.ctx.settings.require_https = False


def test_csp_allows_only_this_origin_the_idp_and_configured_hosts(app):
    s = app.state.ctx.settings
    s.oidc_issuer, s.csp_connect_extra = "https://login.example.com/tenant", "https://tiles.example.org javascript:alert(1)"
    try:
        csp = content_security_policy(s)
    finally:
        s.oidc_issuer, s.csp_connect_extra = None, ""
    assert "script-src 'self'" in csp and "object-src 'none'" in csp and "frame-ancestors 'self'" in csp
    assert "connect-src 'self' https://login.example.com https://tiles.example.org" in csp
    assert "javascript:" not in csp


def test_web_client_gets_the_csp(app, tmp_path, monkeypatch):
    import ajgem.__main__ as main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>AJGEM</title>")
    monkeypatch.setattr(main, "WEB_DIST", dist)
    with TestClient(with_web_client(app)) as c:
        r = c.get("/projects")
        assert r.status_code == 200 and "script-src 'self'" in r.headers["content-security-policy"]
        api = c.get("/api/auth/config")
        assert api.status_code == 200 and "content-security-policy" not in api.headers
