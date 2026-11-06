"""Object storage and malware-scanner abstractions.

``LocalObjectStore`` writes content-addressed, write-once files under a private
directory and stands in for S3-compatible storage. ``SignatureScanner`` only
detects the EICAR test signature; production must plug in a real scanner
(e.g. ClamAV) — until then versions are marked ``scan_status='signature_only'``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path


class LocalObjectStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "chunks").mkdir(parents=True, exist_ok=True)
        (self.root / "quarantine").mkdir(parents=True, exist_ok=True)

    # --- upload chunks -------------------------------------------------
    def chunk_path(self, session_id: str, offset: int) -> Path:
        d = self.root / "chunks" / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{offset:012d}"

    def write_chunk(self, session_id: str, offset: int, data: bytes) -> None:
        self.chunk_path(session_id, offset).write_bytes(data)

    def assemble(self, session_id: str) -> bytes:
        d = self.root / "chunks" / session_id
        parts = sorted(d.iterdir()) if d.exists() else []
        out = bytearray()
        for p in parts:
            offset = int(p.name)
            data = p.read_bytes()
            if offset > len(out):
                raise ValueError("gap in chunks")
            out[offset : offset + len(data)] = data
        return bytes(out)

    def drop_chunks(self, session_id: str) -> None:
        d = self.root / "chunks" / session_id
        if d.exists():
            for p in d.iterdir():
                p.unlink()
            d.rmdir()

    # --- immutable objects --------------------------------------------
    def put_immutable(self, organisation_id: str, sha256: str, data: bytes, quarantine: bool = False) -> str:
        area = "quarantine" if quarantine else "objects"
        # Tenant-scoped key: identical bytes in two organisations are stored separately,
        # so deduplication is never observable across tenants (FR04).
        key = f"{area}/{organisation_id}/{sha256}"
        path = self.root / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.chmod(tmp, 0o440)
            tmp.replace(path)
        return key

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def delete(self, key: str) -> None:
        p = self.root / key
        if p.exists():
            os.chmod(p, 0o640)
            p.unlink()


class SignatureScanner:
    name = "signature_only"
    _EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

    def scan(self, data: bytes) -> tuple[bool, str]:
        if self._EICAR in data:
            return False, "EICAR test signature"
        return True, self.name


class UrlSigner:
    """Short-lived, scope-bound view links (FRD §9 'short-lived scoped object-storage links')."""

    def __init__(self, secret: bytes, ttl_seconds: int = 300):
        self.secret = secret
        self.ttl = ttl_seconds

    def sign(self, version_id: str, user_id: str, now: float | None = None) -> str:
        exp = int((now or time.time()) + self.ttl)
        mac = hmac.new(self.secret, f"{version_id}:{user_id}:{exp}".encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{mac}"

    def verify(self, version_id: str, user_id: str, token: str, now: float | None = None) -> bool:
        try:
            exp_s, mac = token.split(".", 1)
            exp = int(exp_s)
        except ValueError:
            return False
        if exp < (now or time.time()):
            return False
        good = hmac.new(self.secret, f"{version_id}:{user_id}:{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, mac)
