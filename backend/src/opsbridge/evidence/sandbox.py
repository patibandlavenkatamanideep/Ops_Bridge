"""Confinement for the subprocesses that parse untrusted files (production review A06).

pypdf, Pillow and tesseract read files someone outside wrote. Their subprocesses already get
none of the service's secrets in their environment (parsers.PARSER_ENVIRONMENT). This takes
away the rest of what a flaw reached through such a file would find: the files the service
can read (the delegation signing key, other tenants' artifacts) and its network (the
database, the MCP services, the identity provider, DNS).

Two layers, both of which an unprivileged process can apply to itself and every process it
starts:
- Landlock. A confined parser reads only beneath the paths it is given: for the PDF worker
  its own Python installation and code, and the system libraries; for tesseract also the one
  page it reads. It executes only beneath the paths it is given, and writes, creates or
  removes nothing. From Landlock ABI 4 (Linux 6.7) it can neither bind nor connect a TCP
  socket; from ABI 6 it cannot reach abstract Unix sockets or signal outside its domain.
- A seccomp filter. `socket()` and `io_uring_setup()` (io_uring can open sockets without
  `socket()`) fail with EACCES, so no UDP (DNS), no Unix socket with a path and no other
  family either. A system call made through another ABI (32-bit or x32), which would get
  around the filter's numbers, kills the process.

Descriptors it already holds, its stdin and stdout pipes, keep working. Where a layer is
unavailable (macOS; Linux before 5.13 for Landlock, 6.7 for its TCP rules; a seccomp profile
that refuses it), the policy decides: `required` refuses to parse unless both hold,
`best_effort` parses with whatever holds. Development is best effort; a deployed service
requires the sandbox unless told otherwise (Settings.parser_sandbox_policy).

`python -I -m opsbridge.evidence.sandbox probe FILE TCP UDP UNIX [--layers L]` confines itself
as the PDF worker does (or with only the named layers), then reports whether it could read
FILE, connect to TCP or send to UDP on 127.0.0.1, or connect to the Unix socket UNIX, and
whether it could still load its code. `self_test()` runs it against a file and sockets of
its own. `... exec POLICY [--read=PATH]... -- PROGRAM ARG...` confines itself, then runs
PROGRAM in its place (how tesseract is started).
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SandboxPolicy = Literal["required", "best_effort"]

# System calls (the same number on every architecture) and their constants.
_CREATE_RULESET, _ADD_RULE, _RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_O_PATH: int = getattr(os, "O_PATH", 0)  # Linux only, as is everything that uses it
# Filesystem access rights; each ABI handles more of them (see _handled_fs).
_FS_EXECUTE, _FS_READ_FILE, _FS_READ_DIR = 1 << 0, 1 << 2, 1 << 3
_NET_BIND_AND_CONNECT_TCP = (1 << 0) | (1 << 1)  # ABI 4
_SCOPE_ABSTRACT_UNIX_AND_SIGNAL = (1 << 0) | (1 << 1)  # ABI 6
# seccomp: a classic BPF program over struct seccomp_data (nr at offset 0, arch at 4).
_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER = 22, 2
_LD_W_ABS, _JEQ_K, _JGE_K, _RET_K = 0x20, 0x15, 0x35, 0x06
_RET_KILL_PROCESS, _RET_ERRNO, _RET_ALLOW = 0x80000000, 0x00050000, 0x7FFF0000
_X32_SYSCALL_BIT = 0x40000000
_IO_URING_SETUP = 425  # the same on both architectures below
# Architecture: (AUDIT_ARCH value, socket's number, whether the x32 ABI exists).
_ARCHITECTURES = {"x86_64": (0xC000003E, 41, True), "aarch64": (0xC00000B7, 198, False)}

# Public system files a confined process may read: shared libraries the loader opens late,
# its cache, and (for tesseract) its language data. Nothing secret lives beneath them.
SYSTEM_READ = ("/usr", "/lib", "/lib64", "/bin", "/etc/ld.so.cache")
SYSTEM_EXEC = ("/usr", "/lib", "/lib64", "/bin")


class Unavailable(RuntimeError):
    """This kernel or platform cannot confine the process."""


@dataclass(frozen=True)
class Confinement:
    abi: int  # Landlock ABI applied; 0 when Landlock did not hold
    sockets: bool = False  # the seccomp filter refuses new sockets
    missing: tuple[str, ...] = ()  # why a layer did not hold

    @property
    def tcp(self) -> bool:
        return self.abi >= 4

    def describe(self) -> str:
        layers = []
        if self.abi:
            covered = ["files"]
            if self.tcp:
                covered.append("TCP")
            if self.abi >= 6:
                covered.append("abstract sockets and signals")
            layers.append(f"landlock/{self.abi} ({', '.join(covered)})")
        if self.sockets:
            layers.append("seccomp (no new sockets)")
        return " + ".join(layers) or "none"

    def problems(self) -> list[str]:
        """What keeps it from being the full sandbox a `required` policy asks for."""
        found = []
        if not self.abi:
            found.append("no Landlock")
        elif not self.tcp:
            found.append(
                f"Landlock ABI {self.abi} cannot restrict TCP (Linux 6.7 or later is needed)"
            )
        if not self.sockets:
            found.append("no socket filter")
        return [*found, *self.missing] if found else []


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def _libc() -> ctypes.CDLL:
    if not sys.platform.startswith("linux"):
        raise Unavailable(f"Landlock and seccomp are Linux's; this is {sys.platform}")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def _failure(what: str, landlock: bool = True) -> Unavailable:
    err = ctypes.get_errno()
    reasons = {
        errno.ENOSYS: "the kernel has no Landlock",
        errno.EOPNOTSUPP: "Landlock is disabled at boot",
        errno.EPERM: "Landlock is refused here (a seccomp profile?)",
    }
    reason = reasons.get(err) if landlock else None
    return Unavailable(f"{what}: {reason or os.strerror(err)}")


def abi() -> int:
    """The kernel's Landlock ABI version; raises Unavailable."""
    libc = _libc()
    version = libc.syscall(
        ctypes.c_long(_CREATE_RULESET),
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint32(_CREATE_RULESET_VERSION),
    )
    if version < 0:
        raise _failure("no Landlock")
    return int(version)


def _handled_fs(version: int) -> int:
    # ABI 1 handles 13 rights; 2 adds REFER, 3 TRUNCATE, 5 IOCTL_DEV.
    return (1 << (13 + (version >= 2) + (version >= 3) + (version >= 5))) - 1


def _rules(readable: Iterable[str | Path], executable: Iterable[str | Path]) -> dict[Path, int]:
    """The rights each path is granted. A path that is missing or cannot be inspected is
    left out: it is then not granted, never more."""
    rights: dict[Path, int] = {}
    for paths, granted in ((readable, 0), (executable, _FS_EXECUTE)):
        for raw in paths:
            path = Path(raw)
            try:
                if not path.exists():
                    continue
                directory = path.is_dir()
            except OSError:
                continue
            access = granted | _FS_READ_FILE | (_FS_READ_DIR if directory else 0)
            if not directory:
                access &= _FS_EXECUTE | _FS_READ_FILE
            rights[path] = rights.get(path, 0) | access
    return rights


def _landlock(
    libc: ctypes.CDLL, readable: Iterable[str | Path], executable: Iterable[str | Path]
) -> int:
    version = abi()
    attr = _RulesetAttr(
        handled_access_fs=_handled_fs(version),
        handled_access_net=_NET_BIND_AND_CONNECT_TCP if version >= 4 else 0,
        scoped=_SCOPE_ABSTRACT_UNIX_AND_SIGNAL if version >= 6 else 0,
    )
    size = 8 if version < 4 else 16 if version < 6 else 24
    ruleset = libc.syscall(
        ctypes.c_long(_CREATE_RULESET),
        ctypes.byref(attr),
        ctypes.c_size_t(size),
        ctypes.c_uint32(0),
    )
    if ruleset < 0:
        raise _failure("cannot create a Landlock ruleset")
    try:
        for path, access in _rules(readable, executable).items():
            try:
                fd = os.open(path, _O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # not granted
            try:
                rule = _PathBeneathAttr(allowed_access=access, parent_fd=fd)
                if libc.syscall(
                    ctypes.c_long(_ADD_RULE),
                    ctypes.c_int(ruleset),
                    ctypes.c_int(_RULE_PATH_BENEATH),
                    ctypes.byref(rule),
                    ctypes.c_uint32(0),
                ):
                    raise _failure(f"cannot allow {path}")
            finally:
                os.close(fd)
        if libc.syscall(ctypes.c_long(_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)):
            raise _failure("cannot restrict this process")
    finally:
        os.close(ruleset)
    return version


def _socket_filter(
    arch: int, refused: tuple[int, ...], x32: bool
) -> list[tuple[int, int, int, int]]:
    """BPF: kill a call through any other ABI; refuse `refused` with EACCES; allow the rest."""
    checks = [(_JGE_K, _X32_SYSCALL_BIT)] if x32 else []
    checks += [(_JEQ_K, nr) for nr in refused]
    program = [
        (_LD_W_ABS, 0, 0, 4),  # arch
        (_JEQ_K, 1, 0, arch),
        (_RET_K, 0, 0, _RET_KILL_PROCESS),
        (_LD_W_ABS, 0, 0, 0),  # nr
    ]
    # Each check jumps over the checks after it and the allow, to the refusal.
    program += [(code, len(checks) - i, 0, k) for i, (code, k) in enumerate(checks)]
    program += [(_RET_K, 0, 0, _RET_ALLOW), (_RET_K, 0, 0, _RET_ERRNO | errno.EACCES)]
    return program


def _deny_sockets(libc: ctypes.CDLL) -> None:
    machine = platform.machine()
    if machine not in _ARCHITECTURES:
        raise Unavailable(f"no socket filter for {machine}")
    arch, socket_nr, x32 = _ARCHITECTURES[machine]
    program = _socket_filter(arch, (socket_nr, _IO_URING_SETUP), x32)
    filters = (_SockFilter * len(program))(*(_SockFilter(*i) for i in program))
    prog = _SockFprog(len(program), ctypes.cast(filters, ctypes.POINTER(_SockFilter)))
    zero = ctypes.c_ulong(0)
    if libc.prctl(
        ctypes.c_int(_PR_SET_SECCOMP),
        ctypes.c_ulong(_SECCOMP_MODE_FILTER),
        ctypes.byref(prog),
        zero,
        zero,
    ):
        raise _failure("cannot install the socket filter", landlock=False)


def confine(
    readable: Iterable[str | Path],
    executable: Iterable[str | Path] = (),
    layers: Iterable[str] = ("landlock", "seccomp"),
) -> Confinement:
    """Restrict this process and every process it starts: with Landlock, to reading beneath
    `readable` and executing beneath `executable`, with no other file access and (ABI 4+) no
    TCP bind or connect; with seccomp, to creating no socket. Each layer applies where it
    can; raises Unavailable, having restricted nothing, where neither can."""
    libc = _libc()
    one, zero = ctypes.c_ulong(1), ctypes.c_ulong(0)
    if libc.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), one, zero, zero, zero):
        raise _failure("cannot set no_new_privs", landlock=False)
    version, sockets, missing = 0, False, []
    wanted = set(layers)
    if "landlock" in wanted:
        try:
            version = _landlock(libc, readable, executable)
        except Unavailable as exc:
            missing.append(str(exc))
    if "seccomp" in wanted:
        try:
            _deny_sockets(libc)
            sockets = True
        except Unavailable as exc:
            missing.append(str(exc))
    if not version and not sockets:
        raise Unavailable("; ".join(missing) or "no layer was asked for")
    return Confinement(abi=version, sockets=sockets, missing=tuple(missing))


def worker_paths() -> list[str]:
    """What the PDF worker may read: its Python installation and code, and system files."""
    return [*(p for p in sys.path if p), *SYSTEM_READ]


@dataclass(frozen=True)
class Entry:
    sandbox: str  # what holds: "landlock/7 (files, TCP, …)", or "none (why)"
    refusal: str | None  # why the process must not go on, under a required policy


def enter(
    policy: str, readable: Iterable[str | Path], executable: Iterable[str | Path] = ()
) -> Entry:
    """Confine this process under `policy`. Any policy but `best_effort` counts as required:
    then anything short of Landlock with TCP rules and the socket filter is a refusal."""
    try:
        confinement = confine(readable, executable)
    except Unavailable as exc:
        refusal = None if policy == "best_effort" else f"the parser sandbox is unavailable: {exc}"
        return Entry(f"none ({exc})", refusal)
    problems = confinement.problems()
    if policy != "best_effort" and problems:
        return Entry(
            confinement.describe(), "the parser sandbox does not fully hold: " + "; ".join(problems)
        )
    return Entry(confinement.describe(), None)


def _attempt(action: Callable[[], object], done: str) -> str:
    try:
        action()
    except PermissionError:
        return "denied"
    except OSError as exc:
        return f"error: {type(exc).__name__}"
    return done


def _udp(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.sendto(b"probe", ("127.0.0.1", port))


def _unix(path: str) -> None:
    with socket.socket(socket.AF_UNIX) as unix:
        unix.connect(path)


def _io_uring() -> None:
    """Set up an io_uring, which can open sockets without `socket()` (Linux 5.19+)."""
    libc = _libc()
    params = (ctypes.c_char * 120)()  # struct io_uring_params
    fd = libc.syscall(ctypes.c_long(_IO_URING_SETUP), ctypes.c_uint32(1), ctypes.byref(params))
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))  # EACCES and EPERM raise PermissionError
    os.close(fd)


def _probe(target: str, tcp: int, udp: int, unix: str, layers: Iterable[str]) -> dict[str, str]:
    wanted = [layer for layer in layers if layer]
    if wanted:
        try:
            report = {"sandbox": confine(worker_paths(), layers=wanted).describe()}
        except Unavailable as exc:
            return {"unavailable": str(exc)}
    else:  # the control: unconfined, every attempt below should succeed
        report = {"sandbox": "none (unconfined control)"}
    report["read"] = _attempt(lambda: Path(target).read_bytes(), "read")
    report["tcp"] = _attempt(
        lambda: socket.create_connection(("127.0.0.1", tcp), timeout=5).close(), "connected"
    )
    report["udp"] = _attempt(lambda: _udp(udp), "sent")
    report["unix"] = _attempt(lambda: _unix(unix), "connected")
    report["io_uring"] = (
        _attempt(_io_uring, "set up") if sys.platform.startswith("linux") else "not Linux"
    )
    try:
        import encodings.cp1252  # noqa: F401 - loaded late, as pypdf's text decoding does

        import PIL.Image  # noqa: F401
        import pypdf  # noqa: F401

        report["import"] = "ok"
    except Exception as exc:
        report["import"] = f"failed: {type(exc).__name__}"
    return report


def run_probe(
    layers: Iterable[str] = ("landlock", "seccomp"), timeout: float = 30.0
) -> dict[str, str]:
    """Run the probe, confined with `layers`, against a file, a TCP listener, a UDP socket
    and a Unix socket of this process's."""
    with (
        tempfile.TemporaryDirectory() as tmp,
        socket.socket() as tcp,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
    ):
        target = Path(tmp) / "probe"
        target.write_text("outside the parser's code")
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(4)
        udp.bind(("127.0.0.1", 0))
        unix_path = str(Path(tmp) / "probe.sock")
        unix = None
        if hasattr(socket, "AF_UNIX"):
            unix = socket.socket(socket.AF_UNIX)
            unix.bind(unix_path)
            unix.listen(4)
        try:
            proc = subprocess.run(  # noqa: S603 - this interpreter and this module
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "opsbridge.evidence.sandbox",
                    "probe",
                    str(target),
                    str(tcp.getsockname()[1]),
                    str(udp.getsockname()[1]),
                    unix_path,
                    f"--layers={','.join(layers)}",
                ],
                capture_output=True,
                timeout=timeout,
                check=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except subprocess.TimeoutExpired:
            return {"unavailable": "the sandbox probe timed out"}
        finally:
            if unix is not None:
                unix.close()
    try:
        return dict(json.loads(proc.stdout))
    except ValueError:
        return {"unavailable": f"the sandbox probe failed (exit {proc.returncode})"}


def verdict(report: dict[str, str]) -> str | None:
    """None when a fully confined probe could read no file outside its code, reach no TCP,
    UDP or Unix socket, and still load its code; otherwise what went wrong."""
    if "unavailable" in report:
        return report["unavailable"]
    if report["read"] != "denied":
        return f"a confined parser could still read a file outside its code ({report['read']})"
    for kind in ("tcp", "udp", "unix"):
        if report[kind] != "denied":
            return f"a confined parser could still use a {kind.upper()} socket ({report[kind]})"
    if report["io_uring"] != "denied":
        return f"a confined parser could still set up an io_uring ({report['io_uring']})"
    if report["import"] != "ok":
        return f"a confined parser could no longer load its code ({report['import']})"
    return None


def self_test(timeout: float = 30.0) -> str | None:
    """Confine a probe as the PDF worker is confined; see `verdict`."""
    return verdict(run_probe(timeout=timeout))


def _exec(argv: list[str]) -> int:
    """exec POLICY [--read=PATH]... -- PROGRAM [ARG]..."""
    if "--" not in argv or not argv or argv[0] == "--":
        print("usage: exec POLICY [--read=PATH]... -- PROGRAM [ARG]...", file=sys.stderr)
        return 2
    split = argv.index("--")
    policy, options, command = argv[0], argv[1:split], argv[split + 1 :]
    if not command or any(not o.startswith("--read=") for o in options):
        print("usage: exec POLICY [--read=PATH]... -- PROGRAM [ARG]...", file=sys.stderr)
        return 2
    readable = [o.removeprefix("--read=") for o in options]
    program = command[0]
    entry = enter(policy, [*SYSTEM_READ, *readable, program], [*SYSTEM_EXEC, program])
    if entry.refusal:
        print(entry.refusal, file=sys.stderr)
        return 126
    os.execv(program, command)  # noqa: S606 - the caller's fixed argument vector


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["exec"]:
        return _exec(args[1:])
    parser = argparse.ArgumentParser(prog="python -m opsbridge.evidence.sandbox")
    sub = parser.add_subparsers(dest="mode", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("target")
    probe.add_argument("tcp", type=int)
    probe.add_argument("udp", type=int)
    probe.add_argument("unix")
    probe.add_argument("--layers", default="landlock,seccomp")
    parsed = parser.parse_args(args)
    report = _probe(parsed.target, parsed.tcp, parsed.udp, parsed.unix, parsed.layers.split(","))
    sys.stdout.write(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
