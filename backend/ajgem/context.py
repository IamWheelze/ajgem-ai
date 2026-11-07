"""Runtime settings and shared services."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .services.storage import LocalObjectStore, SignatureScanner, UrlSigner


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("AJGEM_DATABASE_URL", "sqlite:///./var/ajgem.db"))
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
    # Development login picker (lists users). Must be disabled wherever real identity is used.
    dev_login: bool = field(default_factory=lambda: os.environ.get("AJGEM_DEV_LOGIN", "1") == "1")


@dataclass
class Context:
    settings: Settings
    store: LocalObjectStore
    scanner: SignatureScanner
    signer: UrlSigner

    @classmethod
    def from_settings(cls, settings: Settings) -> "Context":
        return cls(
            settings=settings,
            store=LocalObjectStore(settings.data_dir),
            scanner=SignatureScanner(),
            signer=UrlSigner(settings.url_secret),
        )
