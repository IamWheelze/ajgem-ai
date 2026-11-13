"""NF05 parser sandbox: resource limits, no network, and how a limit shows up in processing."""

from __future__ import annotations

import os
import socket
import subprocess

import pytest

from ajgem.services import sandbox

from .conftest import make_pdf, make_scanned_pdf


def _userns_available() -> bool:
    r = subprocess.run(["python", "-c", "import os; os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)"],
                       capture_output=True)
    return r.returncode == 0


USERNS = _userns_available()


def _seccomp_available() -> bool:
    from ajgem.services import seccomp

    pid = os.fork()
    if pid == 0:  # the filter is irreversible, so probe it in a throwaway child
        os._exit(0 if seccomp.install() else 1)
    return os.waitpid(pid, 0)[1] == 0


SECCOMP = _seccomp_available()


# Module-level so the fork server can import them.
def _connect_out():
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        return "connected"
    except OSError as e:
        return e.__class__.__name__


def _allocate(n):
    return len(bytearray(n))


def _spin():
    while True:
        pass


def _raise():
    raise KeyError("from the child")


def _crash():
    os.abort()


def test_result_and_exceptions_cross_the_boundary():
    assert sandbox.run(_allocate, 1000)[0] == 1000
    with pytest.raises(KeyError, match="from the child"):
        sandbox.run(_raise)
    with pytest.raises(sandbox.SandboxError):
        sandbox.run(_crash)


def test_memory_cpu_and_wall_limits():
    with pytest.raises(sandbox.ParserLimitExceeded) as e:
        sandbox.run(_allocate, 3 * 1024**3, limits=sandbox.Limits(memory_bytes=1024**3))
    assert e.value.code == "PARSER_MEMORY_LIMIT"
    with pytest.raises(sandbox.ParserLimitExceeded) as e:
        sandbox.run(_spin, limits=sandbox.Limits(cpu_seconds=1, wall_seconds=30))
    assert e.value.code == "PARSER_CPU_LIMIT"
    with pytest.raises(sandbox.ParserLimitExceeded) as e:
        sandbox.run(_spin, limits=sandbox.Limits(cpu_seconds=60, wall_seconds=1))
    assert e.value.code == "PARSER_TIMEOUT"


@pytest.mark.skipif(not USERNS, reason="unprivileged user namespaces are disabled on this host")
def test_parser_has_no_network():
    out, isolated = sandbox.run(_connect_out)
    assert isolated is True and out != "connected"


def _forbidden_calls():
    import socket
    import subprocess

    out = {}
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
        out["inet_socket"] = "allowed"
    except PermissionError:
        out["inet_socket"] = "refused"
    try:
        os.unshare(os.CLONE_NEWNS)
        out["unshare"] = "allowed"
    except PermissionError:
        out["unshare"] = "refused"
    a, b = socket.socketpair()  # Unix sockets stay available (the parser's own plumbing)
    a.close(), b.close()
    out["subprocess"] = subprocess.run(["true"]).returncode  # the OCR engine is started as a child process
    return out


@pytest.mark.skipif(not SECCOMP, reason="seccomp filtering is not available on this host")
def test_seccomp_refuses_syscalls_a_parser_never_needs():
    out, iso = sandbox.run_with_status(_forbidden_calls)
    assert iso["seccomp"] is True
    assert out == {"inet_socket": "refused", "unshare": "refused", "subprocess": 0}


def test_seccomp_program_is_well_formed():
    from ajgem.services import seccomp

    for arch in seccomp.TABLES:
        prog = seccomp.program(arch)
        assert prog[-1] == (0x06, 0, 0, 0x00050001) and prog[-2] == (0x06, 0, 0, 0x7FFF0000)
        for i, (code, jt, jf, _) in enumerate(prog):
            if code in (0x15, 0x35):
                assert i + 1 + max(jt, jf) < len(prog)  # every jump lands inside the program


def test_isolation_is_reported_not_assumed():
    _, isolated = sandbox.run(_allocate, 1)
    assert isolated is USERNS


def test_pages_are_parsed_in_the_sandbox(team, app):
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, make_pdf([["Certificate No: CERT-S", "KW-301 Li 1500 ppm"]]), "s.pdf").json()
    assert an.get(f"/jobs/{up['job_id']}").json()["state"] == "needs_review"
    from ajgem.models import PageText

    with app.state.admin_sessions() as s:
        q = s.query(PageText).filter_by(version_id=up["version_id"]).one().quality
    assert q["sandbox"] == {"version": sandbox.SANDBOX_VERSION, "enabled": True, "network_isolated": USERNS,
                            "seccomp": SECCOMP}


def test_a_parser_limit_flags_the_page_without_retrying(team, app, monkeypatch):
    from ajgem.services import processing

    real = processing.sandbox.run_or_inline_with_status

    def tight(enabled, fn, *args, limits=None, **kw):
        if fn is processing.parse_page:  # only the page parser gets a limit far below what it needs
            limits = sandbox.Limits(memory_bytes=64 * 2**20)
        return real(enabled, fn, *args, limits=limits, **kw)

    monkeypatch.setattr(processing.sandbox, "run_or_inline_with_status", tight)
    an, pid = team["analyst"], team["pid"]
    # Rendering a scanned page at 300 DPI needs far more than 64 MiB.
    up = an.upload(pid, make_scanned_pdf([["KW-302 Li 1500 ppm"]]), "t.pdf").json()
    job = an.get(f"/jobs/{up['job_id']}").json()
    unit = job["manifest"]["units"][0]
    assert unit["state"] == "flagged" and unit["attempts"] == 1  # deterministic: not retried
    assert unit["issues"][0]["code"] == "PARSER_MEMORY_LIMIT" and "manual entry" in unit["issues"][0]["message"]


def test_spreadsheet_parsing_is_bounded_too(team, app):
    """A small file that expands enormously (a zip bomb in spirit) is refused, not allowed to exhaust the server."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SampleID", "Li_ppm"] + [f"pad{i}" for i in range(48)])
    for i in range(40_000):
        ws.append([f"KW-{i}", 100] + ["x" * 20] * 48)
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    assert len(data) < 8 * 2**20  # compresses well; expands to millions of cells
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, data, "big.xlsx").json()
    app.state.ctx.settings.parser_memory_mb = 256
    try:
        r = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": {
            "columns": {"sample_id": "SampleID"}, "analytes": [{"column": "Li_ppm", "analyte": "Li", "unit": "ppm"}]}})
        assert r.status_code == 415 or r.json()["error"]["code"] == "INPUT_UNSUPPORTED", r.text
        assert "parser's limits" in r.json()["error"]["message"]
    finally:
        app.state.ctx.settings.parser_memory_mb = 2048


def test_text_layer_anchors_are_placed_on_the_page(team, app):
    """FR13: a text-layer figure is outlined at the same occurrence the extractor read, not the first match."""
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, make_pdf([["Certificate No: CERT-T", "KW-701 Li 1850 ppm", "KW-702 Li 1850 ppm"]]), "t.pdf").json()
    recs = {r["data"]["sample_id"]: r for r in
            an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]}
    boxes = {}
    for sid in ("KW-701", "KW-702"):
        aid = recs[sid]["field_anchors"]["result_raw"][0]
        v = an.get(f"/sources/{up['version_id']}/anchors/{aid}").json()
        a = v["anchor"]
        assert a["raw_text"] == "1850" and a["bbox_source"] == "text_layer"
        assert a["bbox_convention"] == "pdf_points_top_left"
        boxes[sid] = a["bbox"]
        w, h = v["page"]["size"]
        x0, y0, x1, y1 = a["bbox"]
        assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h and (y1 - y0) < 20
    assert boxes["KW-702"][1] > boxes["KW-701"][1]  # the second line's figure sits lower on the page
    assert abs(boxes["KW-702"][0] - boxes["KW-701"][0]) < 1
    # The page image itself is rendered in the sandbox too.
    img = an.get(v["page"]["image_url"])
    assert img.status_code == 200 and img.content[:8] == b"\x89PNG\r\n\x1a\n"
