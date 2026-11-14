"""NF02 managed identity: OIDC token verification, account linking, MFA for privileged roles, revocation."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import ajgem.auth as auth_mod
from ajgem.api.app import create_app
from ajgem.context import Settings
from ajgem.models import Organisation, User

from .conftest import PG, PG_ADMIN_URL, PG_URL, _reset_pg_schema

ISSUER = "https://idp.example.test"
CLIENT = "ajgem-web"


class FakeIdP:
    def __init__(self):
        self.keys = {}
        self.add_key("k1")
        self.fetches = 0

    def add_key(self, kid):
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def fetch(self, url):
        self.fetches += 1
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": ISSUER, "jwks_uri": ISSUER + "/jwks", "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token", "end_session_endpoint": ISSUER + "/logout"}
        if url.endswith("/jwks"):
            out = []
            for kid, k in self.keys.items():
                jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key()))
                out.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
            return {"keys": out}
        raise AssertionError(url)

    def token(self, sub, email=None, verified=True, amr=("pwd",), kid="k1", iss=ISSUER, aud=CLIENT,
              iat=None, exp_in=3600, key=None, alg="RS256", **extra):
        now = int(time.time()) if iat is None else iat
        claims = {"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + exp_in, "amr": list(amr), **extra}
        if email:
            claims["email"], claims["email_verified"] = email, verified
        return jwt.encode(claims, key or self.keys[kid], algorithm=alg, headers={"kid": kid})


@pytest.fixture()
def idp(monkeypatch):
    f = FakeIdP()
    monkeypatch.setattr(auth_mod, "_fetch_json", f.fetch)
    return f


@pytest.fixture()
def oidc_app(tmp_path, idp):
    common = dict(data_dir=tmp_path / "o", url_secret=b"x", auth_mode="oidc", oidc_issuer=ISSUER, oidc_client_id=CLIENT)
    if PG:
        _reset_pg_schema()
        app = create_app(Settings(database_url=PG_URL, database_admin_url=PG_ADMIN_URL, **common))
    else:
        app = create_app(Settings(database_url="sqlite://", **common))
    with app.state.admin_sessions() as s:
        org = Organisation(name="Kano")
        s.add(org)
        s.flush()
        for email in ("admin@kano.test", "rev@kano.test", "analyst@kano.test"):
            s.add(User(organisation_id=org.id, email=email, name=email.split("@")[0]))
        s.add(Organisation(name="Other"))
        s.commit()
    yield app
    app.state.engine.dispose()
    app.state.admin_engine.dispose()


@pytest.fixture()
def client(oidc_app):
    with TestClient(oidc_app) as c:
        yield c


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def _project(client, idp):
    """Admin (with MFA) creates a project with a reviewer and an analyst."""
    admin = idp.token("sub-admin", "admin@kano.test", amr=["pwd", "mfa"])
    pid = client.post("/projects", json={"name": "P"}, headers=bearer(admin)).json()["id"]
    ids = {}
    for sub, email in (("sub-rev", "rev@kano.test"), ("sub-an", "analyst@kano.test")):
        ids[email] = client.get("/me", headers=bearer(idp.token(sub, email))).json()["id"]
    assert client.post(f"/projects/{pid}/members", json={"user_id": ids["rev@kano.test"], "roles": ["reviewer"]},
                       headers=bearer(admin)).status_code == 201
    client.post(f"/projects/{pid}/members", json={"user_id": ids["analyst@kano.test"], "roles": ["analyst"]},
                headers=bearer(admin))
    return pid, admin, ids


def test_auth_config_exposes_public_client_settings_only(client):
    c = client.get("/auth/config").json()
    assert c == {"mode": "oidc", "issuer": ISSUER, "client_id": CLIENT, "scopes": "openid email profile",
                 "authorization_endpoint": ISSUER + "/authorize", "token_endpoint": ISSUER + "/token",
                 "end_session_endpoint": ISSUER + "/logout"}


def test_first_sign_in_links_by_verified_email_then_uses_the_subject(client, idp, oidc_app):
    me = client.get("/me", headers=bearer(idp.token("sub-1", "analyst@kano.test"))).json()
    assert me["email"] == "analyst@kano.test"
    with oidc_app.state.admin_sessions() as s:
        u = s.query(User).filter_by(email="analyst@kano.test").one()
        assert (u.oidc_issuer, u.oidc_subject) == (ISSUER, "sub-1")
    # Afterwards the subject alone identifies the user, whatever email the token carries.
    assert client.get("/me", headers=bearer(idp.token("sub-1", "renamed@x.test"))).json()["id"] == me["id"]
    # A different subject presenting the same email cannot take over the linked account.
    assert client.get("/me", headers=bearer(idp.token("sub-2", "analyst@kano.test"))).status_code == 401


@pytest.mark.parametrize("case", ["unverified", "unknown", "no_email"])
def test_unregistered_identities_are_refused(client, idp, case):
    tok = {"unverified": idp.token("s", "admin@kano.test", verified=False),
           "unknown": idp.token("s", "stranger@else.test"),
           "no_email": idp.token("s")}[case]
    r = client.get("/me", headers=bearer(tok))
    assert r.status_code == 401 and r.json()["error"]["code"] == "AUTH_REQUIRED"


def test_bad_tokens_are_refused(client, idp):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    bad = {
        "missing": None,
        "garbage": "not.a.jwt",
        "expired": idp.token("s", "admin@kano.test", iat=now - 7200, exp_in=60),
        "wrong_audience": idp.token("s", "admin@kano.test", aud="someone-else"),
        "wrong_issuer": idp.token("s", "admin@kano.test", iss="https://evil.test"),
        "forged_signature": idp.token("s", "admin@kano.test", key=other_key),
        "unknown_kid": idp.token("s", "admin@kano.test", kid="nope", key=other_key),
        "hmac": jwt.encode({"iss": ISSUER, "aud": CLIENT, "sub": "s", "iat": now, "exp": now + 60},
                           "secret", algorithm="HS256", headers={"kid": "k1"}),
        "alg_none": jwt.encode({"iss": ISSUER, "aud": CLIENT, "sub": "s", "iat": now, "exp": now + 60},
                               None, algorithm="none", headers={"kid": "k1"}),
        "too_old_session": idp.token("s", "admin@kano.test", auth_time=now - 13 * 3600),
    }
    for name, tok in bad.items():
        r = client.get("/me", headers=bearer(tok) if tok else {})
        assert r.status_code == 401, name


def test_dev_header_and_user_picker_are_disabled(client, idp, oidc_app):
    with oidc_app.state.admin_sessions() as s:
        uid = s.query(User).filter_by(email="admin@kano.test").one().id
    assert client.get("/me", headers={"X-User-Id": uid}).status_code == 401
    assert client.get("/dev/users").status_code == 403


def test_privileged_actions_need_mfa(client, idp):
    pid, admin, _ids = _project(client, idp)
    an = bearer(idp.token("sub-an", "analyst@kano.test"))
    v = client.post(f"/projects/{pid}/uploads", headers={**an, "Idempotency-Key": "k"}, json={
        "filename": "a.csv", "size": 22, "sha256": "0" * 64, "rights_confirmed": True})
    assert v.status_code == 201  # analysts need no MFA
    rev_pwd = bearer(idp.token("sub-rev", "rev@kano.test", amr=["pwd"]))
    rev_mfa = bearer(idp.token("sub-rev", "rev@kano.test", amr=["pwd", "otp"]))
    assert client.get(f"/projects/{pid}/records", headers=rev_pwd).status_code == 200  # reading is fine
    r = client.get(f"/projects/{pid}/conflicts", headers=rev_pwd)
    assert r.status_code == 200
    blocked = client.post(f"/projects/{pid}/factor-approvals", json={"element": "Li", "oxide": "Li2O"}, headers=rev_pwd)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "MFA_REQUIRED"
    assert client.post(f"/projects/{pid}/factor-approvals", json={"element": "Li", "oxide": "Li2O"},
                       headers=rev_mfa).status_code == 201
    admin_pwd = bearer(idp.token("sub-admin", "admin@kano.test", amr=["pwd"]))
    assert client.get(f"/projects/{pid}/audit", headers=admin_pwd).json()["error"]["code"] == "MFA_REQUIRED"
    assert client.get(f"/projects/{pid}/audit", headers=bearer(admin)).status_code == 200


def test_revocation_and_deactivation_take_effect_immediately(client, idp, oidc_app):
    tok = idp.token("sub-an", "analyst@kano.test", iat=int(time.time()) - 5)
    assert client.get("/me", headers=bearer(tok)).status_code == 200
    with oidc_app.state.admin_sessions() as s:
        u = s.query(User).filter_by(email="analyst@kano.test").one()
        u.sessions_revoked_before = datetime.now(UTC)
        s.commit()
    assert client.get("/me", headers=bearer(tok)).status_code == 401
    fresh = idp.token("sub-an", "analyst@kano.test", iat=int(time.time()) + 1)
    assert client.get("/me", headers=bearer(fresh)).status_code == 200
    with oidc_app.state.admin_sessions() as s:
        s.query(User).filter_by(email="analyst@kano.test").one().active = False
        s.commit()
    assert client.get("/me", headers=bearer(fresh)).status_code == 401


def test_key_rotation_refetches_jwks(client, idp):
    import unittest.mock as m

    assert client.get("/me", headers=bearer(idp.token("sub-an", "analyst@kano.test"))).status_code == 200
    idp.add_key("k2")  # the provider rotates in a new signing key
    tok = idp.token("sub-an", "analyst@kano.test", kid="k2")
    before = idp.fetches
    # Unknown kids trigger a JWKS refetch at most every 10 s; step past that window.
    with m.patch.object(auth_mod.time, "time", return_value=time.time() + 11):
        assert client.get("/me", headers=bearer(tok)).status_code == 200
    assert idp.fetches == before + 1
