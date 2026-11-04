"""Landlock confinement for the subprocesses that parse untrusted files (production review A06).

pypdf, Pillow and tesseract read files someone outside wrote. Their subprocesses already get
none of the service's secrets in their environment (parsers.PARSER_ENVIRONMENT). This takes
away the rest of what a flaw reached through such a file would find: the files the service
can read (the delegation signing key, other tenants' artifacts) and its network (the
database, the MCP services, the identity provider).

Linux's Landlock lets an unprivileged process restrict itself and every process it starts.
A confined parser:
- reads only beneath the paths it is given: for the PDF worker its own Python installation
  and code, and the system libraries; for tesseract also the one page it reads;
- executes only beneath the paths it is given, and writes, creates or removes nothing;
- from Landlock ABI 4 (Linux 6.7), can neither bind nor connect a TCP socket;
- from ABI 6, cannot reach abstract Unix sockets or signal processes outside its domain.

Descriptors it already holds, its stdin and stdout pipes, keep working. Not covered: UDP
(so DNS) and Unix sockets with a path. Where Landlock is unavailable (macOS, Linux before
5.13, Landlock off at boot, or a seccomp profile that refuses it), the policy decides:
`required` refuses to parse, `best_effort` parses unconfined. Development is best effort; a
deployed service requires the sandbox unless told otherwise (Settings.parser_sandbox_policy).

`python -I -m opsbridge.evidence.sandbox probe FILE PORT` confines itself as the PDF worker
does, then reports whether it could read FILE or connect to PORT on 127.0.0.1 and whether it
could still load its code. `self_test()` runs it against a file and a socket of its own.
`... exec POLICY [--read PATH]... -- PROGRAM ARG...` confines itself, then runs PROGRAM in
its place (how tesseract is started).
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterable
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

# Public system files a confined process may read: shared libraries the loader opens late,
# its cache, and (for tesseract) its language data. Nothing secret lives beneath them.
SYSTEM_READ = ("/usr", "/lib", "/lib64", "/bin", "/etc/ld.so.cache")
SYSTEM_EXEC = ("/usr", "/lib", "/lib64", "/bin")


class Unavailable(RuntimeError):
    """This kernel or platform cannot confine the process."""


@dataclass(frozen=True)
class Confinement:
    abi: int

    @property
    def tcp(self) -> bool:
        return self.abi >= 4

    def describe(self) -> str:
        covered = ["files"]
        if self.tcp:
            covered.append("TCP")
        if self.abi >= 6:
            covered.append("abstract sockets and signals")
        return f"landlock/{self.abi} ({', '.join(covered)})"


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc() -> ctypes.CDLL:
    if not sys.platform.startswith("linux"):
        raise Unavailable(f"Landlock is Linux's; this is {sys.platform}")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def _failure(what: str) -> Unavailable:
    err = ctypes.get_errno()
    reason = {
        errno.ENOSYS: "the kernel has no Landlock",
        errno.EOPNOTSUPP: "Landlock is disabled at boot",
        errno.EPERM: "Landlock is refused here (a seccomp profile?)",
    }.get(err, os.strerror(err))
    return Unavailable(f"{what}: {reason}")


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


def confine(readable: Iterable[str | Path], executable: Iterable[str | Path] = ()) -> Confinement:
    """Restrict this process and every process it starts to reading beneath `readable` and
    executing beneath `executable`, with no other file access, and (ABI 4+) no TCP bind or
    connect. Raises Unavailable, before restricting anything, where it cannot."""
    libc = _libc()
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
        one, zero = ctypes.c_ulong(1), ctypes.c_ulong(0)
        if libc.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), one, zero, zero, zero):
            raise _failure("cannot set no_new_privs")
        if libc.syscall(ctypes.c_long(_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)):
            raise _failure("cannot restrict this process")
    finally:
        os.close(ruleset)
    return Confinement(abi=version)


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
    then a platform without Landlock, or without its TCP rules, is a refusal."""
    try:
        confinement = confine(readable, executable)
    except Unavailable as exc:
        refusal = None if policy == "best_effort" else f"the parser sandbox is unavailable: {exc}"
        return Entry(f"none ({exc})", refusal)
    if policy != "best_effort" and not confinement.tcp:
        return Entry(
            confinement.describe(),
            f"the parser sandbox cannot restrict TCP here (Landlock ABI {confinement.abi}; "
            "Linux 6.7 or later is needed)",
        )
    return Entry(confinement.describe(), None)


def _probe(target: str, port: int) -> dict[str, str]:
    try:
        report = {"sandbox": confine(worker_paths()).describe()}
    except Unavailable as exc:
        return {"unavailable": str(exc)}
    try:
        Path(target).read_bytes()
        report["read"] = "read"
    except PermissionError:
        report["read"] = "denied"
    except OSError as exc:
        report["read"] = f"error: {type(exc).__name__}"
    try:
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
        report["connect"] = "connected"
    except PermissionError:
        report["connect"] = "denied"
    except OSError as exc:
        report["connect"] = f"error: {type(exc).__name__}"
    try:
        import encodings.cp1252  # noqa: F401 - loaded late, as pypdf's text decoding does

        import PIL.Image  # noqa: F401
        import pypdf  # noqa: F401

        report["import"] = "ok"
    except Exception as exc:
        report["import"] = f"failed: {type(exc).__name__}"
    return report


def self_test(timeout: float = 30.0) -> str | None:
    """Confine a probe as the PDF worker is confined, against a file and a listening socket
    of this process's. None when it could read neither and still load its code; otherwise
    what went wrong."""
    with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
        target = Path(tmp) / "probe"
        target.write_text("outside the parser's code")
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        try:
            proc = subprocess.run(  # noqa: S603 - this interpreter and this module
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "opsbridge.evidence.sandbox",
                    "probe",
                    str(target),
                    str(listener.getsockname()[1]),
                ],
                capture_output=True,
                timeout=timeout,
                check=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except subprocess.TimeoutExpired:
            return "the sandbox probe timed out"
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        return f"the sandbox probe failed (exit {proc.returncode})"
    if "unavailable" in report:
        return str(report["unavailable"])
    if report["read"] != "denied":
        return f"a confined parser could still read a file outside its code ({report['read']})"
    if report["connect"] != "denied":
        return f"a confined parser could still connect over TCP ({report['connect']})"
    if report["import"] != "ok":
        return f"a confined parser could no longer load its code ({report['import']})"
    return None


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
    probe.add_argument("port", type=int)
    parsed = parser.parse_args(args)
    sys.stdout.write(json.dumps(_probe(parsed.target, parsed.port)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
