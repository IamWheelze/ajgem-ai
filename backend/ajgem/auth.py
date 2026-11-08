"""Managed identity via OpenID Connect (FRD NF02).

The web client signs users in with the organisation's identity provider
(authorisation code + PKCE) and sends the resulting ID token as
``Authorization: Bearer``. This module verifies it:

* signature against the provider's JWKS (RS256/ES256 only; ``none`` and HMAC
  are refused), with key-rotation refresh on an unknown ``kid``;
* ``iss``, ``aud``, ``exp``, ``nbf``, ``iat`` with small clock skew, and a
  maximum session age from ``auth_time``/``iat`` (short-lived sessions);
* MFA from the ``amr`` claim (RFC 8176) or ``acr``; administrators and
  reviewers must have used MFA for privileged operations
  (``services.access``).

Users are never created by sign-in: an account must exist (``create-user``
or an administrator). The first sign-in links the provider subject to the
account with the same *verified* email; afterwards the subject alone is used.
Deactivating a user, revoking their memberships or setting
``sessions_revoked_before`` takes effect on the next request.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

import jwt

from .errors import ApiError

ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]
DEFAULT_MFA_AMR = {"mfa", "otp", "hwk", "fido", "sc", "hwk+otp"}


def _fetch_json(url: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ajgem-oidc"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https URLs from configuration
        return json.loads(r.read().decode())


@dataclass
class OidcSettings:
    issuer: str
    client_id: str
    audience: str | None = None
    jwks_url: str | None = None
    max_session_age_seconds: int = 12 * 3600
    leeway_seconds: int = 60
    mfa_amr: set[str] = field(default_factory=lambda: set(DEFAULT_MFA_AMR))
    mfa_acr: set[str] = field(default_factory=set)
    scopes: str = "openid email profile"


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    mfa: bool
    issued_at: int
    claims: dict


class OidcVerifier:
    def __init__(self, cfg: OidcSettings, fetch=None):
        self.cfg = cfg
        # Resolved at call time so tests (and a future proxy-aware fetcher) can substitute it.
        self._fetch = fetch or (lambda url: _fetch_json(url))
        self._discovery: dict | None = None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ discovery & keys
    def discovery(self) -> dict:
        if self._discovery is None:
            url = self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
            d = self._fetch(url)
            if d.get("issuer", "").rstrip("/") != self.cfg.issuer.rstrip("/"):
                raise ApiError("AUTH_CONFIG", "Identity provider discovery returned a different issuer", status=503)
            self._discovery = d
        return self._discovery

    def _jwks_url(self) -> str:
        return self.cfg.jwks_url or self.discovery()["jwks_uri"]

    def _refresh_keys(self) -> None:
        jwks = self._fetch(self._jwks_url())
        keys = {}
        for k in jwks.get("keys", []):
            if k.get("use", "sig") != "sig" or "kid" not in k:
                continue
            try:
                keys[k["kid"]] = jwt.PyJWK(k)
            except jwt.PyJWKError:
                continue
        self._keys, self._keys_at = keys, time.time()

    def _key(self, kid: str | None) -> jwt.PyJWK:
        with self._lock:
            if kid not in self._keys and time.time() - self._keys_at > 10:  # rotation; rate-limited refetch
                self._refresh_keys()
            if kid not in self._keys:
                raise ApiError("AUTH_REQUIRED", "Sign-in token was signed with an unknown key")
            return self._keys[kid]

    # ------------------------------------------------------------ verification
    def verify(self, token: str) -> Identity:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise ApiError("AUTH_REQUIRED", "Malformed sign-in token") from None
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise ApiError("AUTH_REQUIRED", "Sign-in token uses a disallowed algorithm")
        key = self._key(header.get("kid"))
        try:
            claims = jwt.decode(
                token, key=key, algorithms=[header["alg"]], audience=self.cfg.audience or self.cfg.client_id,
                issuer=self.cfg.issuer, leeway=self.cfg.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise ApiError("AUTH_REQUIRED", "Your session has expired; sign in again") from None
        except jwt.PyJWTError as e:
            raise ApiError("AUTH_REQUIRED", f"Invalid sign-in token ({type(e).__name__})") from None
        started = int(claims.get("auth_time") or claims["iat"])
        if time.time() - started > self.cfg.max_session_age_seconds:
            raise ApiError("AUTH_REQUIRED", "Your session is too old; sign in again")
        amr = {str(a).lower() for a in (claims.get("amr") or [])}
        mfa = bool(amr & {a.lower() for a in self.cfg.mfa_amr}) or (claims.get("acr") in self.cfg.mfa_acr)
        return Identity(issuer=claims["iss"], subject=str(claims["sub"]), email=claims.get("email"),
                        email_verified=claims.get("email_verified") is True, mfa=mfa,
                        issued_at=int(claims["iat"]), claims=claims)

    def client_config(self) -> dict:
        d = self.discovery()
        return {"mode": "oidc", "issuer": self.cfg.issuer, "client_id": self.cfg.client_id, "scopes": self.cfg.scopes,
                "authorization_endpoint": d["authorization_endpoint"], "token_endpoint": d["token_endpoint"],
                "end_session_endpoint": d.get("end_session_endpoint")}


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def resolve_user_id(db, privileged, ident: Identity) -> str:
    """Map a verified identity to a user id (owner role: the organisation is not known yet)."""
    from sqlalchemy import func, select

    from .models import User

    def lookup(s):
        u = s.scalar(select(User).where(User.oidc_issuer == ident.issuer, User.oidc_subject == ident.subject))
        if u is None:
            if not (ident.email and ident.email_verified):
                raise ApiError("AUTH_REQUIRED", "This account is not registered (no verified email to match)")
            matches = s.scalars(select(User).where(func.lower(User.email) == ident.email.lower(),
                                                   User.oidc_subject.is_(None))).all()
            if len(matches) != 1:
                raise ApiError("AUTH_REQUIRED", "This account is not registered with AJGEM; ask an administrator")
            u = matches[0]
            u.oidc_issuer, u.oidc_subject = ident.issuer, ident.subject  # first sign-in links the subject
            s.flush()
        if not u.active:
            raise ApiError("AUTH_REQUIRED", "This account is deactivated")
        cut = u.sessions_revoked_before
        if cut is not None:
            cut = cut if cut.tzinfo else cut.replace(tzinfo=timezone.utc)
            if ident.issued_at < int(cut.timestamp()):
                raise ApiError("AUTH_REQUIRED", "This session was revoked; sign in again")
        return u.id

    if privileged is None:
        return lookup(db)
    with privileged() as s:
        uid = lookup(s)
        s.commit()
        return uid


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
