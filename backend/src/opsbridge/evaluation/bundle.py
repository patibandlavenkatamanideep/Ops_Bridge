"""A versioned, checksummed evaluation evidence bundle (production review A17).

The runtime image carries the evaluation summaries (results, frozen manifests, the signed
ledger and its public key) but not the files the ledger binds: the trial and tool files under
`evals/runs` and the inputs each run used. There `eval-verify` checks the chain and the
signatures only. `eval-bundle` packs the ledger, its public key and every file a ledger record
binds into one archive. It is reproducible: the same files give the same bytes, so its SHA-256
names it. It carries a manifest of every file's SHA-256. `eval-verify --bundle` checks a bundle
against this installation's own ledger and key, then verifies every record's artifacts from
the bundle alone, as `make eval-verify` does in the repository.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from opsbridge.evaluation import receipt

SCHEMA = "opsbridge-evidence-bundle/1"
LEDGER = "evals/ledger/opsbridge-bench.jsonl"
SIGNATURES = "evals/ledger/opsbridge-bench.jsonl.sig.jsonl"
PUBLIC_KEY = "evals/keys/opsbridge-eval.pub"
# What the bundle must share with the installation it is verified for.
LEDGER_FILES = (LEDGER, SIGNATURES, PUBLIC_KEY)
MANIFEST = "BUNDLE.json"
MAX_BUNDLE_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 10_000


class BundleInvalid(ValueError):
    """The bundle is refused before any record is verified from it."""


def _safe(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise BundleInvalid(f"{name!r} is outside the bundle")
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


def _unpack(bundle: Path, into: Path) -> dict[str, Any]:
    if bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise BundleInvalid("the bundle is larger than any this installation accepts")
    try:
        with tarfile.open(bundle, mode="r:gz") as tar:
            entries = tar.getmembers()
            if len(entries) > MAX_MEMBERS:
                raise BundleInvalid(
                    "the bundle has more members than any this installation accepts"
                )
            for m in entries:
                if not m.isfile():
                    raise BundleInvalid(f"{m.name!r} is not a regular file")
                _safe(m.name)
            tar.extractall(into, filter="data")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BundleInvalid(f"the bundle cannot be read: {type(exc).__name__}") from exc
    try:
        manifest = json.loads((into / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleInvalid("the bundle has no readable manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise BundleInvalid(f"the bundle's schema is not {SCHEMA}")
    listed = manifest.get("files")
    present = {m.name for m in entries} - {MANIFEST}
    if not isinstance(listed, dict) or set(listed) != present:
        raise BundleInvalid("the bundle's files and its manifest disagree")
    for name, digest in listed.items():
        if hashlib.sha256((into / name).read_bytes()).hexdigest() != digest:
            raise BundleInvalid(f"{name} does not match the bundle's manifest")
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
