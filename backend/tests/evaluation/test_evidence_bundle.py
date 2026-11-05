"""A versioned, checksummed evidence bundle (production review A17).

The runtime image carries the evaluation summaries but not the files the ledger binds, so
`eval-verify` there checks the chain and signatures only and says the rest is in the
repository. A bundle carries the ledger, its public key and every bound file, reproducibly,
so an installation can verify every record's artifacts from the bundle alone.

A bundle comes from elsewhere, so it is read in one pass and refused, before anything large
is allocated or written, when it would expand, list or name more than an installation
accepts (review N04). The limits are reduced here; no test builds a large archive."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

import pytest

from opsbridge import cli
from opsbridge.config import REPO_ROOT
from opsbridge.evaluation import bundle as bundle_module
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


# --- untrusted archives (review N04) ---------------------------------------------------


def pack(entries: list[tuple[str, bytes]], out: Path, *, kind: bytes = tarfile.REGTYPE) -> Path:
    """`entries` in order, repeats allowed; the last one of type `kind`."""
    with tarfile.open(out, "w:gz", format=tarfile.GNU_FORMAT) as tar:
        for i, (name, data) in enumerate(entries):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            if i == len(entries) - 1:
                info.type = kind
            tar.addfile(info, io.BytesIO(data))
    return out


def refused(path: Path, match: str) -> str:
    with pytest.raises(BundleInvalid, match=match) as exc:
        verify_bundle(path, REPO_ROOT)
    message = str(exc.value)
    assert len(message) < 300 and message.isprintable()
    return message


def with_manifest(contents: dict[str, bytes], **changes: object) -> list[tuple[str, bytes]]:
    manifest = json.loads(contents[MANIFEST]) | changes
    return [(MANIFEST, json.dumps(manifest).encode())] + [
        (n, d) for n, d in contents.items() if n != MANIFEST
    ]


def test_a_member_given_twice_is_refused(bundle: Path, tmp_path: Path) -> None:
    """The last copy was the one extracted and checked; another reader may take the first."""
    files = members(bundle)
    entries = list(files.items())
    second_manifest = with_manifest(files, ledger_records=999)[0]
    refused(pack([*entries, second_manifest], tmp_path / "two.tar.gz"), "more than once")
    key = (bundle_module.PUBLIC_KEY, files[bundle_module.PUBLIC_KEY])
    refused(pack([*entries, key], tmp_path / "key.tar.gz"), "more than once")


@pytest.mark.parametrize(
    "alias",
    [
        "./evals/keys/opsbridge-eval.pub",
        "evals//keys/opsbridge-eval.pub",
        "EVALS/keys/opsbridge-eval.pub",
    ],
)
def test_a_name_that_aliases_another_is_refused(bundle: Path, tmp_path: Path, alias: str) -> None:
    files = members(bundle)
    entries = [*files.items(), (alias, files[bundle_module.PUBLIC_KEY])]
    refused(pack(entries, tmp_path / "alias.tar.gz"), "more than once|not a canonical name")


def test_the_manifest_comes_first(bundle: Path, tmp_path: Path) -> None:
    files = members(bundle)
    entries = [(n, d) for n, d in files.items() if n != MANIFEST] + [(MANIFEST, files[MANIFEST])]
    refused(pack(entries, tmp_path / "late.tar.gz"), "does not begin with")


def test_a_file_larger_than_accepted_is_refused_before_it_is_read(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle_module, "MAX_FILE_BYTES", 100_000)
    refused(bundle, "larger than this installation accepts")


def test_a_bundle_expanding_past_the_limit_is_refused(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle_module, "MAX_EXPANDED_BYTES", 1_000_000)  # it expands to ~4 MB
    refused(bundle, "expands past")


def test_a_header_larger_than_accepted_is_refused(bundle: Path, tmp_path: Path) -> None:
    """A long name is held in memory whole while its header is read."""
    files = members(bundle)
    refused(pack([*files.items(), ("x" * 200_000, b"{}")], tmp_path / "long.tar.gz"), "header")


def test_a_bundle_listing_more_files_than_accepted_is_refused(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle_module, "MAX_MEMBERS", 50)  # it holds 131
    refused(bundle, "more files than")


@pytest.mark.parametrize(
    ("manifest", "match"),
    [
        (b"{not json", "manifest is not valid JSON"),
        (
            b'{"schema": "elsewhere/1", "schema": "opsbridge-evidence-bundle/1"}',
            "duplicate JSON member",
        ),
        (b"[]", "schema is not"),
    ],
)
def test_a_malformed_manifest_is_refused(
    bundle: Path, tmp_path: Path, manifest: bytes, match: str
) -> None:
    files = members(bundle)
    entries = [(MANIFEST, manifest)] + [(n, d) for n, d in files.items() if n != MANIFEST]
    refused(pack(entries, tmp_path / "bad.tar.gz"), match)


def test_a_manifest_with_bad_entries_is_refused(bundle: Path, tmp_path: Path) -> None:
    files = members(bundle)
    listed = json.loads(files[MANIFEST])["files"]
    bad_digest = {**listed, bundle_module.PUBLIC_KEY: "XYZ"}
    refused(pack(with_manifest(files, files=bad_digest), tmp_path / "d.tar.gz"), "not a SHA-256")
    no_key = {n: d for n, d in listed.items() if n != bundle_module.PUBLIC_KEY}
    entries = [e for e in with_manifest(files, files=no_key) if e[0] != bundle_module.PUBLIC_KEY]
    refused(pack(entries, tmp_path / "k.tar.gz"), "does not carry the ledger")
    refused(
        pack(with_manifest(files, ledger_records="all"), tmp_path / "r.tar.gz"), "ledger_records"
    )


def test_a_manifest_larger_than_accepted_is_refused(
    bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle_module, "MAX_MANIFEST_BYTES", 1000)  # it is ~20 KB
    refused(bundle, "manifest is larger")


def test_a_sparse_member_is_refused(bundle: Path, tmp_path: Path) -> None:
    entries = [*members(bundle).items(), ("evals/sparse.json", b"{}")]
    refused(pack(entries, tmp_path / "s.tar.gz", kind=tarfile.GNUTYPE_SPARSE), "not a regular file")


def test_broken_compression_and_truncation_are_refused(bundle: Path, tmp_path: Path) -> None:
    raw = bundle.read_bytes()
    cases = {
        "not gzip": b"not a bundle at all",
        "bad checksum": raw[:-8] + bytes(b ^ 0xFF for b in raw[-8:-4]) + raw[-4:],
        "cut short": raw[: len(raw) // 2],
    }
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
        refused(tmp_path / name, "cannot be read")
    plain = gzip.decompress(raw)
    (tmp_path / "trailing").write_bytes(gzip.compress(plain + b"hidden after the end"))
    refused(tmp_path / "trailing", "after its last member")


def test_a_refused_bundle_leaves_nothing_extracted(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    files = members(bundle)
    last = list(files)[-1]
    refused(
        pack([*list(files.items())[:-1], (last, files[last] + b"!")], tmp_path / "x.tar.gz"),
        "does not match",
    )
    assert list(scratch.iterdir()) == []


def test_a_name_in_an_error_is_shortened_and_printable(bundle: Path, tmp_path: Path) -> None:
    files = members(bundle)
    message = refused(
        pack([*files.items(), ("../\x1b[2J" + "y" * 5000, b"{}")], tmp_path / "n.tar.gz"),
        "outside the bundle",
    )
    assert "\x1b" not in message
