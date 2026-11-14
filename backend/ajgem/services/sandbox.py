"""Isolated execution for parsers of untrusted files (NF05).

PDF parsing, page rendering and OCR run in a short-lived child process with:

* a CPU-time limit (RLIMIT_CPU) and a wall-clock deadline, after which the child is killed;
* an address-space limit (RLIMIT_AS), which also binds the OCR engine the child starts;
* no network where the kernel allows it: the child unshares into fresh user and network
  namespaces, so it has only a loopback interface that is down. Where unprivileged namespaces
  are not available, this is recorded (``network_isolated: False``), not silently assumed;
* a seccomp filter (``seccomp.py``) that refuses syscalls a parser never needs (ptrace, mount,
  module loading, bpf, keyrings, io_uring, non-Unix sockets, foreign ABIs and more). Where it
  cannot be installed this is recorded too (``seccomp: False``).

The child returns plain data (text, word boxes, flags). It never touches the database or
object storage credentials: the parent decides what to store. Children come from a
``forkserver`` that has already imported the parsers, so starting one is cheap and never forks
the multi-threaded API server.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import resource
import signal
import traceback
from dataclasses import dataclass

SANDBOX_VERSION = "sandbox-2"  # 2: seccomp filter


@dataclass(frozen=True)
class Limits:
    cpu_seconds: int = 300
    memory_bytes: int = 2 * 1024**3
    wall_seconds: float = 600.0


class ParserLimitExceeded(Exception):
    """The parser was stopped by a resource limit. Deterministic for a given file: do not retry blindly."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SandboxError(Exception):
    """The sandboxed parser crashed (e.g. a segfault in a native library)."""


_ctx = None


def _context():
    global _ctx
    if _ctx is None:
        _ctx = mp.get_context("forkserver")
        # Imported once in the fork server; every child then starts with the parsers loaded.
        _ctx.set_forkserver_preload(["ajgem.services.processing", "ajgem.services.ocr", "pypdf", "pypdfium2"])
    return _ctx


def _isolate_network() -> bool:
    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
        return True
    except (OSError, AttributeError):
        return False


def _child(conn, limits: Limits, fn, args, kwargs):
    try:
        net = _isolate_network()
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 5))
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        from . import seccomp

        isolated = {"network": net, "seccomp": seccomp.install()}  # last: it also forbids further unshare()
        try:
            out = fn(*args, **kwargs)
        except MemoryError:
            conn.send(("limit", "PARSER_MEMORY_LIMIT", f"Parser exceeded the {limits.memory_bytes // 2**20} MiB memory limit"))
            return
        except BaseException as e:
            try:
                conn.send(("raise", e, isolated))
            except Exception:  # the exception itself is not picklable
                conn.send(("crash", f"{e.__class__.__name__}: {e}\n{traceback.format_exc(limit=5)}", isolated))
            return
        conn.send(("ok", out, isolated))
    finally:
        conn.close()


def run(fn, *args, limits: Limits | None = None, **kwargs):
    """Run ``fn(*args, **kwargs)`` in an isolated child. Returns ``(result, network_isolated)``."""
    out, iso = run_with_status(fn, *args, limits=limits, **kwargs)
    return out, iso["network"]


def run_with_status(fn, *args, limits: Limits | None = None, **kwargs):
    """Like ``run``, returning ``(result, {"network": bool, "seccomp": bool})``.

    ``fn`` must be a module-level function and its arguments and result picklable. Exceptions
    raised by ``fn`` are re-raised here unchanged, so callers keep their error handling.
    """
    limits = limits or Limits()
    ctx = _context()
    parent, child = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_child, args=(child, limits, fn, args, kwargs), daemon=True)
    p.start()
    child.close()
    try:
        if not parent.poll(limits.wall_seconds):
            p.kill()
            p.join(5)
            raise ParserLimitExceeded("PARSER_TIMEOUT", f"Parser did not finish within {limits.wall_seconds:.0f} s")
        try:
            msg = parent.recv()
        except EOFError:
            msg = None
    finally:
        parent.close()
    p.join(10)
    if p.is_alive():  # pragma: no cover - the child sent a result but hangs on exit
        p.kill()
    if msg is None:
        if p.exitcode in (-signal.SIGXCPU, -signal.SIGKILL):
            raise ParserLimitExceeded("PARSER_CPU_LIMIT", f"Parser exceeded the {limits.cpu_seconds} s CPU limit")
        raise SandboxError(f"Parser process died (exit code {p.exitcode})")
    kind = msg[0]
    if kind == "ok":
        return msg[1], msg[2]
    if kind == "raise":
        raise msg[1]
    if kind == "limit":
        raise ParserLimitExceeded(msg[1], msg[2])
    raise SandboxError(msg[1])


def run_or_inline(enabled: bool, fn, *args, limits: Limits | None = None, **kwargs):
    """Sandboxed when enabled; otherwise in-process (development only). Same return shape as ``run``."""
    if enabled:
        return run(fn, *args, limits=limits, **kwargs)
    return fn(*args, **kwargs), False


def run_or_inline_with_status(enabled: bool, fn, *args, limits: Limits | None = None, **kwargs):
    """``run_or_inline`` that also reports whether the seccomp filter was in force."""
    if enabled:
        return run_with_status(fn, *args, limits=limits, **kwargs)
    return fn(*args, **kwargs), {"network": False, "seccomp": False}
