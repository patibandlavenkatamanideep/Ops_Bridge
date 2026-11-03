"""A versioned, checksummed evidence bundle (production review A17).

The runtime image carries the evaluation summaries but not the files the ledger binds, so
`eval-verify` there checks the chain and signatures only and says the rest is in the
repository. A bundle carries the ledger, its public key and every bound file, reproducibly,
so an installation can verify every record's artifacts from the bundle alone."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from opsbridge import cli
from opsbridge.config import REPO_ROOT
from opsbridge.evaluation.bundle import (
    LEDGER,
    LEDGER_FILES,
    MANIFEST,
    BundleInvalid,
    bound_paths,
    verify_bundle,
    write_bundle,
)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("bundle") / "evidence.tar.gz"
    write_bundle(REPO_ROOT, out)
    return out


def members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}  # type: ignore[union-attr]


def repack(files: dict[str, bytes], out: Path) -> Path:
    with tarfile.open(out, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out


def test_the_bundle_is_reproducible_and_carries_every_bound_file(
    bundle: Path, tmp_path: Path
) -> None:
    again = tmp_path / "again.tar.gz"
    write_bundle(REPO_ROOT, again)
    assert again.read_bytes() == bundle.read_bytes()
    files = members(bundle)
    manifest = json.loads(files[MANIFEST])
    bound = bound_paths((REPO_ROOT / LEDGER).read_text(encoding="utf-8"))
    assert len(bound) > 100 and set(bound) <= set(manifest["files"])
    for name, digest in manifest["files"].items():
        assert hashlib.sha256(files[name]).hexdigest() == digest


def test_every_record_verifies_from_the_bundle_alone(
    bundle: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = verify_bundle(bundle, REPO_ROOT)
    assert report["ok"] and report["artifacts"] and all(r["ok"] for r in report["artifacts"])
    assert cli.main(["eval-verify", "--bundle", str(bundle)]) == 0
    assert "FAILED" not in capsys.readouterr().out


def test_a_changed_file_is_refused_whether_or_not_the_manifest_was_rewritten(
    bundle: Path, tmp_path: Path
) -> None:
    files = members(bundle)
    victim = next(n for n in files if n.startswith("evals/runs/") and n.endswith(".json"))
    files[victim] = files[victim] + b"\n"
    with pytest.raises(BundleInvalid, match="does not match"):
        verify_bundle(repack(files, tmp_path / "changed.tar.gz"), REPO_ROOT)
    # Rewriting the manifest to match leaves the ledger's own digest to catch it.
    manifest = json.loads(files[MANIFEST])
    manifest["files"][victim] = hashlib.sha256(files[victim]).hexdigest()
    files[MANIFEST] = json.dumps(manifest).encode()
    report = verify_bundle(repack(files, tmp_path / "rewritten.tar.gz"), REPO_ROOT)
    assert not report["ok"] and any(not r["ok"] for r in report["artifacts"])


def test_a_bundle_for_another_ledger_is_refused(bundle: Path, tmp_path: Path) -> None:
    installation = tmp_path / "installation"
    for name in LEDGER_FILES:
        (installation / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / name, installation / name)
    ledger = installation / LEDGER
    ledger.write_text("".join(ledger.read_text().splitlines(keepends=True)[:-1]))
    with pytest.raises(BundleInvalid, match="another ledger"):
        verify_bundle(bundle, installation)


@pytest.mark.parametrize("name", ["../escape.json", "/etc/escape.json", "evals/../../escape.json"])
def test_a_member_outside_the_bundle_is_refused(bundle: Path, tmp_path: Path, name: str) -> None:
    files = {**members(bundle), name: b"{}"}
    with pytest.raises(BundleInvalid, match=r"outside the bundle|not a regular file"):
        verify_bundle(repack(files, tmp_path / "unsafe.tar.gz"), REPO_ROOT)
    assert not (tmp_path / "escape.json").exists()


def test_a_link_in_the_bundle_is_refused(bundle: Path, tmp_path: Path) -> None:
    out = tmp_path / "link.tar.gz"
    shutil.copy(bundle, tmp_path / "base.tar.gz")
    with tarfile.open(out, "w:gz") as tar, tarfile.open(bundle) as src:
        for m in src.getmembers():
            tar.addfile(m, src.extractfile(m))
        link = tarfile.TarInfo("evals/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    with pytest.raises(BundleInvalid, match="not a regular file"):
        verify_bundle(out, REPO_ROOT)
