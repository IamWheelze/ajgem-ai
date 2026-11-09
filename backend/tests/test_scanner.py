"""FR03 malware scanning through a ClamAV daemon: INSTREAM protocol, quarantine, and failing closed."""

from __future__ import annotations

import os
import socket
import struct
import threading

import pytest
from ajgem.context import Settings
from ajgem.models import DocumentVersion
from ajgem.services.storage import ClamdScanner, ScannerUnavailable, scanner_from_settings

from .conftest import make_assay_csv

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class FakeClamd:
    """Speaks enough of the clamd protocol (zINSTREAM, zVERSION) to test the client, like the real daemon."""

    def __init__(self, max_stream=10_000_000, reply=None):
        self.max_stream, self.reply, self.streams = max_stream, reply, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def address(self):
        return f"tcp://127.0.0.1:{self.port}"

    def _recv(self, conn, n):
        buf = b""
        while len(buf) < n:
            part = conn.recv(n - len(buf))
            if not part:
                raise ConnectionError
            buf += part
        return buf

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                cmd = b""
                while not cmd.endswith(b"\0"):
                    cmd += conn.recv(1)
                if cmd == b"zVERSION\0":
                    conn.sendall(b"ClamAV 1.4.1/27000/Fake\0")
                    continue
                data, too_big = b"", False
                while True:
                    (n,) = struct.unpack("!L", self._recv(conn, 4))
                    if n == 0:
                        break
                    data += self._recv(conn, n)
                    if len(data) > self.max_stream:
                        too_big = True
                        break
                self.streams.append(len(data))
                if self.reply:
                    conn.sendall(self.reply)
                elif too_big:
                    conn.sendall(b"INSTREAM size limit exceeded. ERROR\0")
                elif EICAR in data:
                    conn.sendall(b"stream: Win.Test.EICAR_HDB-1 FOUND\0")
                else:
                    conn.sendall(b"stream: OK\0")

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the accept() thread so the port really closes
        except OSError:
            pass
        self.sock.close()
        self.thread.join(timeout=5)


@pytest.fixture()
def clamd():
    d = FakeClamd()
    yield d
    d.close()


def test_instream_protocol(clamd):
    s = ClamdScanner(clamd.address)
    assert s.scan(b"hello" * 50_000) == (True, "clamav")
    assert clamd.streams[-1] == 250_000  # sent in several length-prefixed chunks
    assert s.scan(b"prefix " + EICAR) == (False, "ClamAV: Win.Test.EICAR_HDB-1")
    assert s.version().startswith("ClamAV")


def test_scanner_fails_closed():
    s = ClamdScanner("tcp://127.0.0.1:1")  # nothing listens here
    with pytest.raises(ScannerUnavailable):
        s.scan(b"x")
    small = FakeClamd(max_stream=10)
    try:
        with pytest.raises(ScannerUnavailable, match="size limit"):
            ClamdScanner(small.address).scan(b"x" * 100)
    finally:
        small.close()
    odd = FakeClamd(reply=b"stream: Can't allocate memory ERROR\0")
    try:
        with pytest.raises(ScannerUnavailable):
            ClamdScanner(odd.address).scan(b"x")
    finally:
        odd.close()


def test_settings_select_the_scanner(tmp_path):
    assert scanner_from_settings(Settings(data_dir=tmp_path, scanner="signature")).name == "signature_only"
    assert scanner_from_settings(Settings(data_dir=tmp_path, scanner="clamd", clamd_address="unix:///run/clamd.ctl")).name == "clamav"
    for bad in (dict(scanner="clamd"), dict(scanner="clamd", clamd_address="http://x"), dict(scanner="mcafee")):
        with pytest.raises(ValueError):
            scanner_from_settings(Settings(data_dir=tmp_path, **bad))


@pytest.fixture()
def clam_app(team, app, clamd):
    app.state.ctx.scanner = ClamdScanner(clamd.address)
    return app, team["analyst"], team["pid"], team["analyst"].c


def test_uploads_are_scanned_by_clamav(clam_app):
    app, api, pid, c = clam_app
    ok = api.upload(pid, make_assay_csv(3), "a.csv").json()
    with app.state.admin_sessions() as s:
        assert s.get(DocumentVersion, ok["version_id"]).scan_status == "clamav"
    bad = api.upload(pid, b"SampleID\n" + EICAR + b"\n", "b.csv").json()["error"]
    assert bad["code"] == "FILE_QUARANTINED" and "ClamAV: Win.Test.EICAR_HDB-1" in bad["message"]
    h = c.get("/health").json()
    assert h["status"] == "ok" and h["scanner"] == {"name": "clamav", "status": "ok"}


def test_scanner_outage_refuses_the_upload_and_keeps_it_retryable(clam_app, clamd):
    app, api, pid, c = clam_app
    data = make_assay_csv(4)
    clamd.close()
    r = api.upload(pid, data, "c.csv")
    assert r.status_code == 503 and r.json()["error"]["code"] == "SCAN_UNAVAILABLE"
    with app.state.admin_sessions() as s:
        assert s.query(DocumentVersion).count() == 0  # nothing unscanned is ever stored as a version
    assert c.get("/health").status_code == 503
    # The scanner comes back: completing the same upload session again scans and accepts it.
    back = FakeClamd()
    try:
        app.state.ctx.scanner = ClamdScanner(back.address)
        done = api.upload(pid, data, "c.csv").json()
        assert done["status"] == "stored" and back.streams == [len(data)]
    finally:
        back.close()


@pytest.mark.skipif(not os.environ.get("AJGEM_TEST_CLAMD"), reason="set AJGEM_TEST_CLAMD=tcp://host:port to use a real clamd")
def test_real_clamd():
    s = ClamdScanner(os.environ["AJGEM_TEST_CLAMD"])
    assert s.version().startswith("ClamAV")
    assert s.scan(make_assay_csv(50)) == (True, "clamav")
    clean, detail = s.scan(EICAR)  # the standard test file itself, which every signature set detects
    assert not clean and "EICAR" in detail.upper()
