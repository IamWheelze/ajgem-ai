"""Runtime settings and shared services."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .services.llm import provider_from_settings
from .services.storage import ClamdScanner, LocalObjectStore, SignatureScanner, UrlSigner, scanner_from_settings


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("AJGEM_DATABASE_URL", "sqlite:///./var/ajgem.db"))
    # Owner role for schema setup, RLS installation and maintenance tools (PostgreSQL). Defaults to database_url.
    database_admin_url: str | None = field(default_factory=lambda: os.environ.get("AJGEM_DATABASE_ADMIN_URL") or None)
    # Apply pending migrations on start-up (as the owner role). Off: start-up only checks the schema is current.
    auto_migrate: bool = field(default_factory=lambda: os.environ.get("AJGEM_AUTO_MIGRATE", "1") == "1")
    # Refuse to serve requests on PostgreSQL with a role that bypasses RLS, unless explicitly allowed.
    allow_rls_bypass: bool = field(default_factory=lambda: os.environ.get("AJGEM_ALLOW_RLS_BYPASS") == "1")
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("AJGEM_DATA_DIR", "./var/objects")))
    url_secret: bytes = field(
        default_factory=lambda: os.environ.get("AJGEM_URL_SECRET", "").encode() or secrets.token_bytes(32)
    )
    # Engineering defaults from FRD §5 — to be load-tested, not vendor limits.
    max_file_bytes: int = 100 * 1024 * 1024
    max_pdf_pages: int = 500
    max_table_rows: int = 100_000
    max_chunk_bytes: int = 8 * 1024 * 1024
    upload_session_days: int = 7
    task_retries: int = 3
    retry_backoff_seconds: float = 0.5
    query_row_limit: int = 50_000
    ocr_timeout_seconds: int = 120
    # Language-model adapter for narrative answers (FR17). "none" disables generation entirely.
    llm_provider: str = field(default_factory=lambda: os.environ.get("AJGEM_LLM_PROVIDER", "none"))
    llm_model: str = field(default_factory=lambda: os.environ.get("AJGEM_LLM_MODEL", "claude-opus-5-5"))
    llm_effort: str = field(default_factory=lambda: os.environ.get("AJGEM_LLM_EFFORT", "medium"))
    llm_timeout_seconds: float = field(default_factory=lambda: float(os.environ.get("AJGEM_LLM_TIMEOUT", "60")))
    llm_max_output_tokens: int = 4000
    # Server-side refusal fallback mode ("default", or "" to disable).
    llm_fallbacks: str = field(default_factory=lambda: os.environ.get("AJGEM_LLM_FALLBACKS", "default"))
    # Authentication: "dev" (X-User-Id header + user picker; never with real data) or "oidc" (NF02).
    auth_mode: str = field(default_factory=lambda: os.environ.get("AJGEM_AUTH_MODE", "dev"))
    oidc_issuer: str | None = field(default_factory=lambda: os.environ.get("AJGEM_OIDC_ISSUER") or None)
    oidc_client_id: str | None = field(default_factory=lambda: os.environ.get("AJGEM_OIDC_CLIENT_ID") or None)
    oidc_audience: str | None = field(default_factory=lambda: os.environ.get("AJGEM_OIDC_AUDIENCE") or None)
    oidc_jwks_url: str | None = field(default_factory=lambda: os.environ.get("AJGEM_OIDC_JWKS_URL") or None)
    oidc_mfa_amr: str = field(default_factory=lambda: os.environ.get("AJGEM_OIDC_MFA_AMR", "mfa,otp,hwk,fido,sc"))
    oidc_mfa_acr: str = field(default_factory=lambda: os.environ.get("AJGEM_OIDC_MFA_ACR", ""))
    oidc_max_session_hours: float = field(default_factory=lambda: float(os.environ.get("AJGEM_OIDC_MAX_SESSION_HOURS", "12")))
    oidc_scopes: str = field(default_factory=lambda: os.environ.get("AJGEM_OIDC_SCOPES", "openid email profile"))
    # Malware scanning (FR03): "signature" (EICAR only, development) or "clamd" (ClamAV daemon; fails closed).
    scanner: str = field(default_factory=lambda: os.environ.get("AJGEM_SCANNER", "signature"))
    clamd_address: str | None = field(default_factory=lambda: os.environ.get("AJGEM_CLAMD_ADDRESS") or None)
    scan_timeout_seconds: float = field(default_factory=lambda: float(os.environ.get("AJGEM_SCAN_TIMEOUT", "120")))
    # Parser sandbox for untrusted files (NF05): child process with CPU, memory and network limits.
    parser_sandbox: bool = field(default_factory=lambda: os.environ.get("AJGEM_PARSER_SANDBOX", "1") == "1")
    parser_cpu_seconds: int = field(default_factory=lambda: int(os.environ.get("AJGEM_PARSER_CPU_SECONDS", "300")))
    parser_memory_mb: int = field(default_factory=lambda: int(os.environ.get("AJGEM_PARSER_MEMORY_MB", "2048")))
    # Job execution (FR05): "inline" runs PDF jobs inside the request (development, tests); "worker" only
    # queues them for `python -m ajgem worker` processes, which hold a renewable lease per job.
    job_executor: str = field(default_factory=lambda: os.environ.get("AJGEM_JOB_EXECUTOR", "inline"))
    job_lease_seconds: int = field(default_factory=lambda: int(os.environ.get("AJGEM_JOB_LEASE_SECONDS", "300")))
    job_max_claims: int = 5
    # FR20: with the worker executor, exports expected to exceed this many rows are built as background jobs.
    export_inline_rows: int = field(default_factory=lambda: int(os.environ.get("AJGEM_EXPORT_INLINE_ROWS", "20000")))
    worker_poll_seconds: float = field(default_factory=lambda: float(os.environ.get("AJGEM_WORKER_POLL_SECONDS", "2")))
    # Operator metrics (NF08): GET /metrics is disabled unless a bearer token is configured.
    metrics_token: str | None = field(default_factory=lambda: os.environ.get("AJGEM_METRICS_TOKEN") or None)
    # Transport hardening (NF03): refuse plain HTTP; extra origins the web client may call (e.g. the basemap host).
    require_https: bool = field(default_factory=lambda: os.environ.get("AJGEM_REQUIRE_HTTPS", "0") == "1")
    csp_connect_extra: str = field(default_factory=lambda: os.environ.get("AJGEM_CSP_CONNECT_EXTRA", ""))
    # Development login picker (lists users). Must be disabled wherever real identity is used.
    dev_login: bool = field(default_factory=lambda: os.environ.get("AJGEM_DEV_LOGIN", "1") == "1")


@dataclass
class Context:
    settings: Settings
    store: LocalObjectStore
    scanner: SignatureScanner | ClamdScanner
    signer: UrlSigner
    answer_provider: object | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "Context":
        return cls(
            settings=settings,
            store=LocalObjectStore(settings.data_dir),
            scanner=scanner_from_settings(settings),
            signer=UrlSigner(settings.url_secret),
            answer_provider=provider_from_settings(settings),
        )
