"""Transport and browser hardening (NF03 service side).

* Security headers on every response: a Content Security Policy for the web client (scripts only from
  this origin; network calls only to this origin, the identity provider and any configured map host),
  no MIME sniffing, no referrer (view links carry short-lived tokens), framing only by this origin, and
  HSTS when the request arrived over HTTPS.
* ``AJGEM_REQUIRE_HTTPS=1``: plain-HTTP requests are refused (API) or redirected (web client). Behind a
  TLS-terminating proxy the scheme is read from ``X-Forwarded-Proto``. ``/health`` stays reachable for
  probes.
* API responses are marked ``Cache-Control: no-store`` unless they set their own caching.

Database, object-store and backup encryption with managed keys are deployment settings; see
docs/OPERATIONS.md.
"""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else None


def content_security_policy(settings) -> str:
    extra = [o for o in (settings.csp_connect_extra or "").split() if o.startswith(("https://", "http://"))]
    connect = ["'self'", *filter(None, [_origin(settings.oidc_issuer)]), *extra]
    images = ["'self'", "data:", "blob:", *extra]
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",  # React style attributes
        f"img-src {' '.join(images)}",
        "font-src 'self' data:",
        f"connect-src {' '.join(dict.fromkeys(connect))}",
        "worker-src 'self' blob:",  # MapLibre's workers
        "child-src 'self' blob:",
        "frame-src 'self'",  # the source viewer embeds originals from this origin
        "frame-ancestors 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ])


def scheme_of(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-proto")
    return (fwd.split(",")[0].strip().lower() if fwd else request.url.scheme) or "http"


def install(app, settings, *, api: bool) -> None:
    csp = content_security_policy(settings)

    @app.middleware("http")
    async def harden(request: Request, call_next):
        https = scheme_of(request) == "https"
        if settings.require_https and not https and not request.url.path.rstrip("/").endswith("/health"):
            if api:
                return JSONResponse({"error": {"code": "HTTPS_REQUIRED", "message": "Use HTTPS"}}, status_code=403)
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        resp = await call_next(request)
        h = resp.headers
        if h.get("content-type", "").startswith("text/html"):
            # Documents only: originals (PDF), page images and JSON are not scripted, and a policy on a PDF can
            # stop the browser's own viewer.
            h.setdefault("Content-Security-Policy", csp)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if https:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if api:
            h.setdefault("Cache-Control", "no-store")
        return resp
