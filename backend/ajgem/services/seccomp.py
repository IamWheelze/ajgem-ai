"""A seccomp-BPF syscall filter for the parser sandbox (NF05).

Installed in the sandbox child after its namespaces and resource limits, it makes the kernel refuse
(EPERM) syscalls a document parser has no business making:

* debugging and reading other processes (ptrace, process_vm_readv/writev, kcmp);
* changing the system or escaping the sandbox (mount family, pivot_root, chroot, setns, unshare,
  kernel modules, kexec, reboot, swap, clock changes, quotas, accounting);
* risky kernel interfaces commonly used in exploits (bpf, perf_event_open, userfaultfd, keyrings,
  io_uring, open_by_handle_at);
* sockets other than Unix sockets, on top of the empty network namespace.

Syscalls from a foreign ABI (x32 on x86_64, or 32-bit compat), which would bypass a filter keyed on
syscall numbers, are refused too. The filter is a deny list, so the OCR engine and image libraries
work unchanged; ``no_new_privs`` is set first, as the kernel requires, and the filter is inherited by
anything the parser starts. Supported on x86_64 and aarch64; elsewhere ``install`` returns False and
the caller records that, rather than assuming protection.
"""

from __future__ import annotations

import ctypes
import errno
import platform
import struct

# seccomp_data offsets
_NR, _ARCH, _ARG0 = 0, 4, 16
_LD_W_ABS, _JEQ_K, _JGE_K, _RET_K = 0x20, 0x15, 0x35, 0x06
_ALLOW = 0x7FFF0000
_ERRNO = 0x00050000 | errno.EPERM
_PR_SET_NO_NEW_PRIVS, _PR_SET_SECCOMP, _SECCOMP_MODE_FILTER = 38, 22, 2
_AF_UNIX = 1

# Names shared by both tables; numbers per architecture (asm/unistd).
_NEW = {"io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427, "open_tree": 428, "move_mount": 429,
        "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433, "mount_setattr": 442}
TABLES = {
    "x86_64": {"audit_arch": 0xC000003E, "socket": 41, "x32_bit": 0x40000000, "deny": {
        "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311, "kcmp": 312,
        "mount": 165, "umount2": 166, "pivot_root": 155, "chroot": 161, "setns": 308, "unshare": 272,
        "init_module": 175, "finit_module": 313, "delete_module": 176, "kexec_load": 246, "kexec_file_load": 320,
        "reboot": 169, "swapon": 167, "swapoff": 168, "settimeofday": 164, "clock_settime": 227, "adjtimex": 159,
        "clock_adjtime": 305, "quotactl": 179, "acct": 163, "bpf": 321, "perf_event_open": 298, "userfaultfd": 323,
        "add_key": 248, "request_key": 249, "keyctl": 250, "open_by_handle_at": 304, "name_to_handle_at": 303,
        "fanotify_init": 300, "iopl": 172, "ioperm": 173, **_NEW}},
    "aarch64": {"audit_arch": 0xC00000B7, "socket": 198, "x32_bit": None, "deny": {
        "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271, "kcmp": 272,
        "mount": 40, "umount2": 39, "pivot_root": 41, "chroot": 51, "setns": 268, "unshare": 97,
        "init_module": 105, "finit_module": 273, "delete_module": 106, "kexec_load": 104, "kexec_file_load": 294,
        "reboot": 142, "swapon": 224, "swapoff": 225, "settimeofday": 170, "clock_settime": 112, "adjtimex": 171,
        "clock_adjtime": 266, "quotactl": 60, "acct": 89, "bpf": 280, "perf_event_open": 241, "userfaultfd": 282,
        "add_key": 217, "request_key": 218, "keyctl": 219, "open_by_handle_at": 265, "name_to_handle_at": 264,
        "fanotify_init": 262, **_NEW}},
}


def _arch() -> str | None:
    m = platform.machine().lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(m)


def program(arch: str) -> list[tuple[int, int, int, int]]:
    """The BPF program as (code, jt, jf, k) instructions. Every refusal jumps to the final ERRNO return."""
    t = TABLES[arch]
    body: list[tuple] = [("ld", _ARCH), ("jne_deny", t["audit_arch"]), ("ld", _NR)]
    if t["x32_bit"]:
        body.append(("jge_deny", t["x32_bit"]))
    body += [("jeq_deny", nr) for nr in sorted(set(t["deny"].values()))]
    body += [("jne_allow_skip", t["socket"]), ("ld", _ARG0), ("jeq_allow", _AF_UNIX), ("ret", _ERRNO),
             ("ret", _ALLOW), ("ret", _ERRNO)]
    allow_at, deny_at = len(body) - 2, len(body) - 1
    out = []
    for i, (op, k) in enumerate(body):
        to_deny, to_allow = deny_at - i - 1, allow_at - i - 1
        if op == "ld":
            out.append((_LD_W_ABS, 0, 0, k))
        elif op == "ret":
            out.append((_RET_K, 0, 0, k))
        elif op == "jne_deny":
            out.append((_JEQ_K, 0, to_deny, k))
        elif op == "jge_deny":
            out.append((_JGE_K, to_deny, 0, k))
        elif op == "jeq_deny":
            out.append((_JEQ_K, to_deny, 0, k))
        elif op == "jne_allow_skip":  # not socket(): allow
            out.append((_JEQ_K, 0, to_allow, k))
        elif op == "jeq_allow":
            out.append((_JEQ_K, to_allow, 0, k))
    assert all(jt < 256 and jf < 256 for _, jt, jf, _ in out)
    return out


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def install() -> bool:
    """Install the filter in the current process. Irreversible. False when unsupported or refused."""
    arch = _arch()
    if arch is None:
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prog = program(arch)
        raw = b"".join(struct.pack("HBBI", *ins) for ins in prog)
        buf = ctypes.create_string_buffer(raw, len(raw))
        fprog = _SockFprog(len(prog), ctypes.cast(buf, ctypes.c_void_p))
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return False
        return libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) == 0
    except (OSError, AttributeError):
        return False
