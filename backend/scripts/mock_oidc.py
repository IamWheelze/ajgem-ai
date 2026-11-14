"""DEVELOPMENT-ONLY mock OpenID Connect provider for exercising AJGEM's sign-in locally and in CI.

    cd backend && python scripts/mock_oidc.py          # http://127.0.0.1:9000

Then run the API with:
    AJGEM_AUTH_MODE=oidc AJGEM_OIDC_ISSUER=http://127.0.0.1:9000 AJGEM_OIDC_CLIENT_ID=ajgem-web python -m ajgem serve

It implements just enough of OIDC for a public SPA client: discovery, JWKS, an authorisation page
where you type an email and choose whether to "use a second factor", PKCE (S256) code exchange, and
RS256 ID tokens with email_verified=true and amr ["pwd"] or ["pwd","mfa"], plus rotating refresh
tokens: each refresh token works once and is replaced; presenting a used one revokes the whole family
(reuse detection). MOCK_OIDC_TOKEN_TTL sets the ID token lifetime in seconds (default 3600). Anyone
can sign in as anyone: never expose it beyond localhost and never use it with real data.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import time
import urllib.parse

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

ISSUER = "http://127.0.0.1:9000"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "mock-" + secrets.token_hex(4)
CODES: dict[str, dict] = {}
REFRESH: dict[str, dict] = {}  # token -> {family, grant, used}
REVOKED_FAMILIES: set[str] = set()
TTL = int(os.environ.get("MOCK_OIDC_TOKEN_TTL", "3600"))

app = FastAPI(title="Mock OIDC provider (development only)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/.well-known/openid-configuration")
def discovery():
    return {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks", "end_session_endpoint": f"{ISSUER}/logout",
            "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["RS256"], "subject_types_supported": ["public"]}


@app.get("/jwks")
def jwks():
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    return {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}


@app.get("/authorize", response_class=HTMLResponse)
def authorize_form(request: Request):
    q = dict(request.query_params)
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        return HTMLResponse("Only response_type=code with PKCE S256 is supported", status_code=400)
    hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in q.items())
    return f"""<!doctype html><meta name=viewport content="width=device-width"><title>Mock sign-in</title>
<body style="font:15px system-ui;max-width:420px;margin:60px auto;padding:0 16px">
<h2>Mock identity provider</h2><p style="color:#a00">Development only: no password is checked.</p>
<form method=post action="/authorize">{hidden}
<label>Email<br><input name=email type=email required style="width:100%;padding:8px" autofocus></label>
<p><label><input type=checkbox name=mfa value=1> Use a second factor (MFA)</label></p>
<button style="padding:8px 16px">Sign in</button></form></body>"""


@app.post("/authorize")
def authorize(email: str = Form(...), mfa: str | None = Form(None), client_id: str = Form(...),
              redirect_uri: str = Form(...), state: str = Form(""), nonce: str = Form(""),
              code_challenge: str = Form(...), scope: str = Form(""), response_type: str = Form("code"),
              code_challenge_method: str = Form("S256")):
    code = secrets.token_urlsafe(24)
    CODES[code] = {"client_id": client_id, "redirect_uri": redirect_uri, "nonce": nonce, "challenge": code_challenge,
                   "email": email.strip().lower(), "mfa": bool(mfa), "at": time.time()}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urllib.parse.urlencode({'code': code, 'state': state})}", 302)


def _issue(grant: dict, family: str, nonce: str | None) -> dict:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": grant["client_id"], "sub": "mock|" + grant["email"], "email": grant["email"],
              "email_verified": True, "iat": now, "auth_time": grant["auth_time"], "exp": now + TTL,
              "amr": ["pwd", "mfa"] if grant["mfa"] else ["pwd"]}
    if nonce:
        claims["nonce"] = nonce
    id_token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})
    refresh = secrets.token_urlsafe(32)
    REFRESH[refresh] = {"family": family, "grant": grant, "used": False}
    return {"id_token": id_token, "access_token": id_token, "token_type": "Bearer", "expires_in": TTL,
            "refresh_token": refresh}


@app.post("/token")
def token(grant_type: str = Form(...), code: str | None = Form(None), redirect_uri: str | None = Form(None),
          client_id: str = Form(...), code_verifier: str | None = Form(None), refresh_token: str | None = Form(None)):
    if grant_type == "refresh_token":
        r = REFRESH.get(refresh_token or "")
        if r is None or r["family"] in REVOKED_FAMILIES or r["grant"]["client_id"] != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if r["used"]:  # replayed: someone else holds a copy, so end the whole sign-in
            REVOKED_FAMILIES.add(r["family"])
            return JSONResponse({"error": "invalid_grant", "error_description": "refresh token reuse"}, status_code=400)
        r["used"] = True
        return _issue(r["grant"], r["family"], None)
    c = CODES.pop(code or "", None)
    if grant_type != "authorization_code" or c is None or time.time() - c["at"] > 120:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    challenge = base64.urlsafe_b64encode(hashlib.sha256((code_verifier or "").encode()).digest()).rstrip(b"=").decode()
    if challenge != c["challenge"] or client_id != c["client_id"] or redirect_uri != c["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE or client mismatch"}, status_code=400)
    grant = {"client_id": client_id, "email": c["email"], "mfa": c["mfa"], "auth_time": int(time.time())}
    return _issue(grant, secrets.token_hex(8), c["nonce"])


@app.get("/logout")
def logout(post_logout_redirect_uri: str = "/"):
    return RedirectResponse(post_logout_redirect_uri, 302)


if __name__ == "__main__":
    print("Mock OIDC provider — DEVELOPMENT ONLY — anyone can sign in as anyone.")
    uvicorn.run(app, host="127.0.0.1", port=9000, log_level="warning")
