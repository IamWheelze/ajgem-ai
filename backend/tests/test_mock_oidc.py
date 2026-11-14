"""The development IdP rotates refresh tokens and revokes the family on reuse, so the client's rotation is tested
against the behaviour real providers (Auth0, Okta, Entra with rotation) show."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import pathlib

import jwt
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("mock_oidc", pathlib.Path(__file__).parents[1] / "scripts" / "mock_oidc.py")
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)


def _sign_in(c: TestClient) -> dict:
    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.post("/authorize", data={"email": "geo@kano.test", "client_id": "ajgem-web", "redirect_uri": "http://x/cb",
                                   "state": "s", "nonce": "n1", "code_challenge": challenge}, follow_redirects=False)
    code = r.headers["location"].split("code=")[1].split("&")[0]
    return c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": "http://x/cb",
                                  "client_id": "ajgem-web", "code_verifier": verifier}).json()


def test_refresh_tokens_rotate_and_reuse_revokes_the_family():
    c = TestClient(mock.app)
    first = _sign_in(c)
    assert first["refresh_token"] and jwt.decode(first["id_token"], options={"verify_signature": False})["nonce"] == "n1"
    def refresh(t):
        return c.post("/token", data={"grant_type": "refresh_token", "refresh_token": t, "client_id": "ajgem-web"})

    second = refresh(first["refresh_token"]).json()
    assert second["refresh_token"] != first["refresh_token"]
    claims = jwt.decode(second["id_token"], options={"verify_signature": False})
    assert claims["email"] == "geo@kano.test" and "nonce" not in claims
    third = refresh(second["refresh_token"]).json()
    # Replaying an already-used token fails and ends the whole sign-in, including the newest token.
    assert refresh(first["refresh_token"]).status_code == 400
    assert refresh(third["refresh_token"]).status_code == 400
    assert refresh("unknown").status_code == 400
