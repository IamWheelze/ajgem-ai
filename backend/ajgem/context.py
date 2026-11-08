"""Runtime settings and shared services."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .services.llm import provider_from_settings
from .services.storage import LocalObjectStore, SignatureScanner, UrlSigner


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("AJGEM_DATABASE_URL", "sqlite:///./var/ajgem.db"))
    # Owner role for schema setup, RLS installation and maintenance tools (PostgreSQL). Defaults to database_url.
    database_admin_url: str | None = field(default_factory=lambda: os.environ.get("AJGEM_DATABASE_ADMIN_URL") or None)
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
    # Development login picker (lists users). Must be disabled wherever real identity is used.
    dev_login: bool = field(default_factory=lambda: os.environ.get("AJGEM_DEV_LOGIN", "1") == "1")


@dataclass
class Context:
    settings: Settings
    store: LocalObjectStore
    scanner: SignatureScanner
    signer: UrlSigner
    answer_provider: object | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "Context":
        return cls(
            settings=settings,
            store=LocalObjectStore(settings.data_dir),
            scanner=SignatureScanner(),
            signer=UrlSigner(settings.url_secret),
            answer_provider=provider_from_settings(settings),
        )
