"""A versioned, checksummed evaluation evidence bundle (production review A17).

The runtime image carries the evaluation summaries (results, frozen manifests, the signed
ledger and its public key) but not the files the ledger binds: the trial and tool files under
`evals/runs` and the inputs each run used. There `eval-verify` checks the chain and the
signatures only. `eval-bundle` packs the ledger, its public key and every file a ledger record
binds into one archive. It is reproducible: the same files give the same bytes, so its SHA-256
names it. It carries a manifest of every file's SHA-256. `eval-verify --bundle` checks a bundle
against this installation's own ledger and key, then verifies every record's artifacts from
the bundle alone, as `make eval-verify` does in the repository.

A bundle comes from elsewhere (review N04). It used to be listed whole and extracted whole
before anything was checked: its compressed size was capped, but not what it expanded to,
nor any one file; a member given twice was extracted twice, the last copy winning; a sparse
member passed as a file; the manifest was decoded without a size limit; and the gzip
checksum was never reached. Now it is read in one pass. The manifest comes first and is
checked (strict JSON, schema, digests, the ledger files) before any other member is read;
then each member must be a regular file under a canonical name the manifest lists, given
once (names compared in Unicode form C and case-folded, since a file system may merge them),
within the per-file limit, and its bytes are counted and hashed as they are copied. What the
archive expands to is counted as it is decompressed, including headers: one member's headers
may not exceed MAX_HEADER_BYTES, nor the whole MAX_EXPANDED_BYTES. The stream is read to its
end, so its checksum is verified, and nothing may follow the last member but padding. A
refusal is a BundleInvalid with a short, printable message; the caller's temporary directory
is removed with whatever was written.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
import tempfile
import unicodedata
import zlib
from pathlib import Path, PurePosixPath
from typing import IO, Any

from opsbridge import safejson
from opsbridge.evaluation import receipt

SCHEMA = "opsbridge-evidence-bundle/1"
LEDGER = "evals/ledger/opsbridge-bench.jsonl"
SIGNATURES = "evals/ledger/opsbridge-bench.jsonl.sig.jsonl"
PUBLIC_KEY = "evals/keys/opsbridge-eval.pub"
# What the bundle must share with the installation it is verified for.
LEDGER_FILES = (LEDGER, SIGNATURES, PUBLIC_KEY)
MANIFEST = "BUNDLE.json"
# The limits, against the bundle of 2026-11-05: 224 KB, 131 files, 3.9 MB expanded, the
# largest file 365 KB, the manifest 20 KB.
MAX_BUNDLE_BYTES = 200 * 1024 * 1024  # as read from disk
MAX_EXPANDED_BYTES = 256 * 1024 * 1024  # everything it decompresses to
MAX_FILE_BYTES = 32 * 1024 * 1024  # any one file
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024  # one member's headers (a long name is one), and the end padding
MAX_MEMBERS = 10_000
_CHUNK = 64 * 1024
_SLACK = 2 * tarfile.RECORDSIZE  # what the tar reader may read ahead of the member it reads
_SHA = re.compile(r"^[0-9a-f]{64}$")


class BundleInvalid(ValueError):
    """The bundle is refused before any record is verified from it."""


def _shown(name: object) -> str:
    text = ascii(name)
    return text if len(text) <= 80 else text[:77] + "..."


def _safe(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise BundleInvalid(f"{_shown(name)} is outside the bundle")
    return path.as_posix()


def bound_paths(ledger_text: str) -> list[str]:
    """Every file a ledger record binds: its artifacts and its dataset."""
    paths: set[str] = set()
    for line in ledger_text.splitlines():
        if not line.strip():
            continue
        manifest = json.loads(line)["manifest"]
        paths.update(a["path"] for a in manifest.get("artifacts", []))
        if manifest.get("dataset", {}).get("path"):
            paths.add(manifest["dataset"]["path"])
    return sorted(_safe(p) for p in paths)


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), 0, 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def write_bundle(root: Path, out: Path) -> str:
    """Write the bundle for the ledger under `root`; returns its SHA-256."""
    root = root.resolve()
    ledger = (root / LEDGER).read_text(encoding="utf-8")
    names = sorted({*LEDGER_FILES, *bound_paths(ledger)})
    files: dict[str, bytes] = {}
    for name in names:
        path = (root / name).resolve()
        if root not in path.parents or not path.is_file():
            raise BundleInvalid(f"bound file {name!r} is not a file in {root}")
        files[name] = path.read_bytes()
    manifest = {
        "schema": SCHEMA,
        "ledger_records": sum(1 for line in ledger.splitlines() if line.strip()),
        "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with (
        out.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar,
    ):
        _add(tar, MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode())
        for name in names:
            _add(tar, name, files[name])
    return hashlib.sha256(out.read_bytes()).hexdigest()


class _Expanded(io.RawIOBase):
    """The decompressed archive, read within an allowance for what is read now (one member's
    headers, or its data) and a limit for the whole."""

    def __init__(self, raw: IO[bytes]) -> None:
        self._gz = gzip.GzipFile(fileobj=raw, mode="rb")
        self.count = 0
        self._until = 0
        self._what = ""

    def allow(self, more: int, what: str) -> None:
        self._until, self._what = self.count + more, what

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        room = min(len(b), _CHUNK, self._until - self.count)
        if room <= 0:
            raise BundleInvalid(f"{self._what} is larger than this installation accepts")
        data = self._gz.read(room)
        self.count += len(data)
        if self.count > MAX_EXPANDED_BYTES:
            raise BundleInvalid(f"the bundle expands past {MAX_EXPANDED_BYTES} bytes")
        b[: len(data)] = data
        return len(data)


def _regular(m: tarfile.TarInfo) -> str:
    """The member's name, when it is a regular file under a canonical name in the bundle."""
    if m.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):  # no links, devices, sparse files
        raise BundleInvalid(f"{_shown(m.name)} is not a regular file")
    name = _safe(m.name)
    if name != m.name:
        raise BundleInvalid(f"{_shown(m.name)} is not a canonical name (it is {_shown(name)})")
    return name


def _manifest(data: bytes) -> dict[str, Any]:
    try:
        manifest = safejson.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BundleInvalid(f"the bundle's manifest is not valid JSON: {str(exc)[:80]}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise BundleInvalid(f"the bundle's schema is not {SCHEMA}")
    records = manifest.get("ledger_records")
    if not isinstance(records, int) or isinstance(records, bool) or records < 0:
        raise BundleInvalid("the bundle's manifest has no count of ledger_records")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise BundleInvalid("the bundle's manifest lists no files")
    if len(files) >= MAX_MEMBERS:
        raise BundleInvalid("the bundle lists more files than this installation accepts")
    for name, digest in files.items():
        if _safe(name) != name or name == MANIFEST:
            raise BundleInvalid(f"the bundle's manifest lists {_shown(name)}, not a file name")
        if not isinstance(digest, str) or not _SHA.match(digest):
            raise BundleInvalid(f"the manifest's digest for {_shown(name)} is not a SHA-256")
    if not set(LEDGER_FILES) <= set(files):
        raise BundleInvalid("the bundle does not carry the ledger and its public key")
    return manifest


def _copy(tar: tarfile.TarFile, m: tarfile.TarInfo, out: IO[bytes]) -> str:
    """Copy the member's data to `out`, counting and hashing it; its SHA-256."""
    source = tar.extractfile(m)
    if source is None:
        raise BundleInvalid(f"{_shown(m.name)} is not a regular file")
    digest, copied = hashlib.sha256(), 0
    with source:
        while chunk := source.read(_CHUNK):
            copied += len(chunk)
            digest.update(chunk)
            out.write(chunk)
    if copied != m.size:
        raise BundleInvalid(f"{_shown(m.name)} is shorter than its header says")
    return digest.hexdigest()


def _members(tar: tarfile.TarFile, stream: _Expanded, into: Path) -> dict[str, Any]:
    stream.allow(MAX_HEADER_BYTES, "a member's header")
    first = tar.next()
    if first is None or first.name != MANIFEST:
        raise BundleInvalid(f"the bundle does not begin with {MANIFEST}")
    _regular(first)
    if first.size > MAX_MANIFEST_BYTES:
        raise BundleInvalid("the bundle's manifest is larger than this installation accepts")
    stream.allow(first.size + _SLACK, MANIFEST)
    data = io.BytesIO()
    _copy(tar, first, data)
    manifest = _manifest(data.getvalue())
    listed: dict[str, str] = manifest["files"]
    seen = {MANIFEST.casefold()}
    written: set[str] = set()
    while True:
        stream.allow(MAX_HEADER_BYTES, "a member's header")
        m = tar.next()
        if m is None:
            break
        name = _regular(m)
        alias = unicodedata.normalize("NFC", name).casefold()
        if alias in seen:
            raise BundleInvalid(f"{_shown(name)} is in the bundle more than once")
        seen.add(alias)
        if name not in listed:
            raise BundleInvalid(f"{_shown(name)} is not in the bundle's manifest")
        if m.size > MAX_FILE_BYTES:
            raise BundleInvalid(f"{_shown(name)} is larger than this installation accepts")
        stream.allow(m.size + _SLACK, _shown(name))
        target = into / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as out:
            copied = _copy(tar, m, out)
        if copied != listed[name]:
            raise BundleInvalid(f"{_shown(name)} does not match the bundle's manifest")
        written.add(name)
    if written != set(listed):
        raise BundleInvalid("the bundle's manifest lists files the bundle does not carry")
    return manifest


def _unpack(bundle: Path, into: Path) -> dict[str, Any]:
    if bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise BundleInvalid("the bundle is larger than any this installation accepts")
    try:
        with bundle.open("rb") as raw:
            stream = _Expanded(raw)
            stream.allow(MAX_HEADER_BYTES, "a member's header")  # opening reads the first
            with tarfile.open(fileobj=stream, mode="r|") as tar:  # one pass, in order
                manifest = _members(tar, stream, into)
            stream.allow(MAX_HEADER_BYTES, "the end of the bundle")
            while rest := stream.read(_CHUNK):  # to the end: the gzip checksum is verified
                if rest.strip(b"\0"):
                    raise BundleInvalid("the bundle has data after its last member")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise BundleInvalid(f"the bundle cannot be read: {type(exc).__name__}") from exc
    return manifest


def verify_bundle(bundle: Path, installation: Path) -> dict[str, Any]:
    """Verify every ledger record's artifacts from `bundle`, which must carry exactly this
    installation's ledger and public key. Raises BundleInvalid before verifying anything when
    the bundle is unsafe, inconsistent or for another ledger."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp)
        _unpack(bundle, tree)
        for name in LEDGER_FILES:
            ours = installation / name
            if not (tree / name).is_file() or (tree / name).read_bytes() != ours.read_bytes():
                raise BundleInvalid(f"the bundle is for another ledger ({name} differs)")
        public_key = (tree / PUBLIC_KEY).read_text(encoding="utf-8").strip()
        return receipt.verify(tree / LEDGER, public_key, artifacts=True, cwd=tree)
