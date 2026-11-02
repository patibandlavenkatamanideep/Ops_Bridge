"""Parser subprocesses read untrusted files, so a flaw in pypdf, Pillow or tesseract must
find neither the deployment's secrets in its environment nor code planted where it was
started (production review A06)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opsbridge.evidence import parsers
from opsbridge.evidence.parsers import OcrSettings, ocr_image, parse

DOCS = Path(__file__).resolve().parents[3] / "fixtures" / "documents"
SECRETS = {
    "OPSBRIDGE_DATABASE_URL": "postgresql://opsbridge:db-secret-1@db/opsbridge",
    "OPSBRIDGE_DELEGATION_PRIVATE_KEY": "private-key-secret-2",
    "ANTHROPIC_API_KEY": "model-secret-3",
}


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)


def test_the_pdf_worker_runs_no_code_from_the_directory_it_was_started_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator may run `export-check` from inside the export, which is untrusted."""
    for module in ("base64", "json"):
        (tmp_path / f"{module}.py").write_text(
            "from pathlib import Path\n"
            "Path(__file__).with_name('ran').write_text(__name__)\n"
            "raise SystemExit(3)\n"
        )
    monkeypatch.chdir(tmp_path)
    doc = parse((DOCS / "cupsfilter_text.pdf").read_bytes(), "application/pdf")
    assert not (tmp_path / "ran").exists()
    assert doc.status == "parsed"


def test_the_pdf_worker_is_given_none_of_the_secrets(
    secrets: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real_run = subprocess.run

    def recording_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append({"argv": argv, **kwargs})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(parsers.subprocess, "run", recording_run)
    # Text mode, then image mode (the OCR path with the engine itself stubbed out).
    text = parse((DOCS / "cupsfilter_text.pdf").read_bytes(), "application/pdf")
    monkeypatch.setattr(parsers, "ocr_unavailable_reason", lambda ocr=None: None)
    monkeypatch.setattr(parsers, "tesseract_version", lambda: "tesseract test-double")
    monkeypatch.setattr(parsers, "ocr_image", lambda *a, **k: "Invoice No: INV-RASTER-1")
    scanned = parse(
        (DOCS / "rasterized_invoice.pdf").read_bytes(),
        "application/pdf",
        OcrSettings(enabled=True),
    )
    assert (text.status, scanned.status) == ("parsed", "needs_review")
    assert scanned.ocr_engine == "tesseract test-double"
    # The scanned PDF is read for a text layer first, then for its page images.
    assert ["images" in c["argv"] for c in calls] == [False, False, True]
    for call in calls:
        env = call.get("env")
        assert env is not None, "the worker inherited the whole environment"
        assert set(env) <= set(parsers.PARSER_ENVIRONMENT)
        assert not set(SECRETS.values()) & set(env.values())


def test_tesseract_is_given_none_of_the_secrets(
    secrets: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in tesseract that prints the environment it was given."""
    fake = tmp_path / "tesseract"
    fake.write_text(
        f"#!{sys.executable} -I\nimport json, os\nprint(json.dumps(dict(os.environ)))\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    seen = ocr_image(b"not read by the stand-in")
    assert seen is not None
    given = json.loads(seen)
    assert given.pop("OMP_THREAD_LIMIT") == "1"
    given.pop("__CF_USER_TEXT_ENCODING", None)  # macOS adds it to every process it starts
    assert set(given) <= set(parsers.PARSER_ENVIRONMENT)
    assert not set(SECRETS.values()) & set(given.values())
