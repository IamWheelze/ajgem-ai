"""Object storage and malware-scanner abstractions.

``LocalObjectStore`` writes content-addressed, write-once files under a private
directory and stands in for S3-compatible storage.

Scanners return ``(clean, detail)``; ``detail`` is stored as the version's ``scan_status``.
``ClamdScanner`` streams the bytes to a ClamAV daemon (INSTREAM) and fails closed: if the
daemon cannot be reached or reports an error, ``ScannerUnavailable`` is raised and the
upload is not accepted. ``SignatureScanner`` only detects the EICAR test signature and is for
development; versions it passes are marked ``scan_status='signature_only'``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import time
from pathlib import Path
from urllib.parse import urlparse


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

    def source(self, key: str) -> str:
        """What a sandboxed parser reads: a local, read-only path (an object store would hand over a download)."""
        return str(self.root / key)

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


class ScannerUnavailable(Exception):
    """The malware scanner could not give a verdict; the upload must not be accepted."""


class ClamdScanner:
    """ClamAV daemon over TCP (``tcp://host:3310``) or a Unix socket (``unix:///run/clamav/clamd.ctl``)."""

    name = "clamav"
    CHUNK = 64 * 1024

    def __init__(self, address: str, timeout_seconds: float = 120.0):
        u = urlparse(address)
        if u.scheme == "tcp" and u.hostname:
            self.family, self.target = socket.AF_INET, (u.hostname, u.port or 3310)
        elif u.scheme == "unix" and u.path:
            self.family, self.target = socket.AF_UNIX, u.path
        else:
            raise ValueError(f"AJGEM_CLAMD_ADDRESS must be tcp://host:port or unix:///path, not {address!r}")
        self.address, self.timeout = address, timeout_seconds

    def _connect(self) -> socket.socket:
        sock = socket.socket(self.family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.target)
        except OSError as e:
            sock.close()
            raise ScannerUnavailable(f"ClamAV daemon at {self.address} is unreachable ({e.__class__.__name__})") from None
        return sock

    @staticmethod
    def _read_reply(sock: socket.socket) -> str:
        buf = b""
        while not buf.endswith(b"\0"):
            part = sock.recv(4096)
            if not part:
                break
            buf += part
        return buf.rstrip(b"\0").decode("utf-8", "replace").strip()

    def version(self) -> str:
        with self._connect() as sock:
            sock.sendall(b"zVERSION\0")
            return self._read_reply(sock)

    def scan(self, data: bytes) -> tuple[bool, str]:
        try:
            with self._connect() as sock:
                sock.sendall(b"zINSTREAM\0")
                for off in range(0, len(data), self.CHUNK):
                    part = data[off: off + self.CHUNK]
                    sock.sendall(struct.pack("!L", len(part)) + part)
                sock.sendall(struct.pack("!L", 0))
                reply = self._read_reply(sock)
        except ScannerUnavailable:
            raise
        except OSError as e:
            raise ScannerUnavailable(f"ClamAV scan failed ({e.__class__.__name__}: {e})") from None
        # Replies: "stream: OK", "stream: <Signature> FOUND", "INSTREAM size limit exceeded. ERROR", "... ERROR".
        if reply.endswith(" OK") or reply == "stream: OK":
            return True, self.name
        if reply.endswith(" FOUND"):
            sig = reply.removeprefix("stream:").removesuffix("FOUND").strip()
            return False, f"ClamAV: {sig}"
        raise ScannerUnavailable(f"ClamAV could not scan the file: {reply or 'no reply'}")


def scanner_from_settings(settings):
    if settings.scanner == "clamd":
        if not settings.clamd_address:
            raise ValueError("AJGEM_SCANNER=clamd needs AJGEM_CLAMD_ADDRESS (tcp://host:port or unix:///path)")
        return ClamdScanner(settings.clamd_address, settings.scan_timeout_seconds)
    if settings.scanner == "signature":
        return SignatureScanner()
    raise ValueError(f"Unknown AJGEM_SCANNER {settings.scanner!r} (use 'clamd' or 'signature')")


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
