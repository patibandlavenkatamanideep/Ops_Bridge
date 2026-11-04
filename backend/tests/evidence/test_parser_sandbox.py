"""Parser subprocesses are confined with Landlock (production review A06).

They already get none of the service's secrets in their environment. A flaw in pypdf, Pillow
or tesseract, reached through a hostile file, still found every file the service can read
(the delegation signing key, other tenants' artifacts) and its network (the database, the
MCP services). Confined, a parser reads only its own code and system libraries, writes
nothing, and can neither bind nor connect a TCP socket.

The Landlock tests run where the kernel offers it (Linux 5.13+; TCP from 6.7) and skip
elsewhere, except with OPSBRIDGE_REQUIRE_SANDBOX=1 (hosted CI), where its absence fails."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opsbridge.config import ConfigurationError, Settings
from opsbridge.evidence import parsers, sandbox
from opsbridge.evidence.parsers import OcrSettings, parse

DOCS = Path(__file__).resolve().parents[3] / "fixtures" / "documents"
TEXT_PDF = (DOCS / "cupsfilter_text.pdf").read_bytes()


def _landlock() -> bool:
    try:
        return sandbox.abi() >= 4
    except sandbox.Unavailable:
        return False


def _require_landlock() -> None:
    if not _landlock():
        if os.environ.get("OPSBRIDGE_REQUIRE_SANDBOX") == "1":
            pytest.fail("OPSBRIDGE_REQUIRE_SANDBOX=1 but Landlock (ABI 4+) is unavailable")
        pytest.skip("Landlock with TCP rules is Linux 6.7+ (runs in hosted CI and the image)")


def _probe(target: Path, port: int) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-I", "-m", "opsbridge.evidence.sandbox", "probe", str(target), str(port)],
        capture_output=True,
        timeout=30,
        check=False,
        env={"PATH": os.environ["PATH"]},
    )
    return dict(json.loads(proc.stdout))


def test_a_confined_parser_reads_no_file_and_reaches_no_port_outside_its_code(
    tmp_path: Path,
) -> None:
    _require_landlock()
    planted = tmp_path / "delegation_ed25519.pem"
    planted.write_text("not for parsers")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        # Unconfined, the same file and port are open to this process.
        assert planted.read_text() == "not for parsers"
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        report = _probe(planted, port)
    assert report["sandbox"].startswith("landlock/")
    assert (report["read"], report["connect"]) == ("denied", "denied")
    # Its own code still loads after confinement (pypdf, Pillow and stdlib codecs load late).
    assert report["import"] == "ok"
    assert sandbox.self_test() is None


def test_the_pdf_worker_confines_itself_before_reading_the_file() -> None:
    _require_landlock()
    for policy in ("required", "best_effort"):
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "opsbridge.evidence.pdf_worker", policy, "50"],
            input=TEXT_PDF,
            capture_output=True,
            timeout=30,
            check=False,
            env={"PATH": os.environ["PATH"]},
        )
        out = json.loads(proc.stdout)
        assert out["status"] == "ok"
        assert out["sandbox"].startswith("landlock/")
    doc = parse(TEXT_PDF, "application/pdf", OcrSettings(sandbox="required"))
    assert doc.status == "parsed"


def test_tesseract_starts_confined_to_the_page_it_reads(tmp_path: Path) -> None:
    _require_landlock()
    given, other = tmp_path / "given", tmp_path / "other"
    given.mkdir()
    other.mkdir()
    (given / "page.png").write_text("the page")
    (other / "secret").write_text("not for tesseract")
    cat = shutil.which("cat")
    assert cat is not None

    def run(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "opsbridge.evidence.sandbox",
                "exec",
                "required",
                f"--read={given}",
                "--",
                cat,
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={"PATH": os.environ["PATH"]},
        )

    assert run(given / "page.png").stdout == "the page"
    refused = run(other / "secret")
    assert refused.returncode != 0 and "not for tesseract" not in refused.stdout


def test_required_refuses_to_parse_where_the_sandbox_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_a: Any, **_k: Any) -> sandbox.Confinement:
        raise sandbox.Unavailable("the kernel has no Landlock")

    monkeypatch.setattr(sandbox, "confine", unavailable)
    assert sandbox.enter("required", []).refusal == (
        "the parser sandbox is unavailable: the kernel has no Landlock"
    )
    assert sandbox.enter("best_effort", []) == sandbox.Entry(
        "none (the kernel has no Landlock)", None
    )
    # Files confined but TCP not (Landlock ABI 1-3) is not enough when it is required.
    monkeypatch.setattr(sandbox, "confine", lambda *_a, **_k: sandbox.Confinement(abi=3))
    assert "TCP" in (sandbox.enter("required", []).refusal or "")
    assert sandbox.enter("best_effort", []).refusal is None
    # An unknown policy is taken as required.
    assert sandbox.enter("strict", []).refusal is not None


def test_where_landlock_is_unavailable_a_required_parse_is_refused() -> None:
    if _landlock():
        pytest.skip("Landlock is available here; the confined tests cover this platform")
    doc = parse(TEXT_PDF, "application/pdf", OcrSettings(sandbox="required"))
    assert doc.status == "rejected"
    assert "parser sandbox is unavailable" in (doc.detail or "")
    assert parse(TEXT_PDF, "application/pdf").status == "parsed"  # best effort (development)


DEPLOYED: dict[str, Any] = {
    "public_url": "https://opsbridge.example.test",
    "oidc_issuer": "https://id.example.test/realms/opsbridge",
    "session_secret": "s" * 40,
    "artifact_s3_bucket": "opsbridge-artifacts",
    "artifact_s3_sse": "aws:kms",
    "trusted_proxies": ["10.0.0.0/16"],
    "demo_user_password": None,
    "simulator_fault_controls": False,
}


def settings(**changes: Any) -> Settings:
    return Settings(_env_file=None, **{**DEPLOYED, **changes})  # type: ignore[call-arg]


def test_a_deployed_environment_requires_the_sandbox_unless_told_otherwise() -> None:
    assert settings(env="staging").parser_sandbox_policy == "required"
    assert settings(env="staging", parser_sandbox="best_effort").parser_sandbox_policy == (
        "best_effort"
    )
    assert Settings(_env_file=None, env="development").parser_sandbox_policy == "best_effort"  # type: ignore[call-arg]
    assert OcrSettings.from_settings(settings(env="staging")).sandbox == "required"


def test_a_deployed_service_refuses_to_start_without_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opsbridge.api.app import create_app
    from opsbridge.workflow.worker import make_worker

    monkeypatch.setattr(sandbox, "self_test", lambda: "the kernel has no Landlock")
    with pytest.raises(ConfigurationError, match="parser sandbox is required in staging"):
        create_app(settings=settings(env="staging"))
    with pytest.raises(ConfigurationError, match="parser sandbox is required in staging"):
        make_worker(settings=settings(env="staging"))
    # Told best_effort, the operator has accepted parsing unconfined.
    parsers.require_sandbox(settings(env="staging", parser_sandbox="best_effort"))


def test_the_check_command_says_whether_the_sandbox_holds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from opsbridge import cli

    code = cli.main(["parser-sandbox-check"])
    out = capsys.readouterr().out
    if _landlock():
        assert code == 0 and out.startswith("parser sandbox holds: landlock/")
    else:
        assert code == 1 and out.startswith("parser sandbox does not hold: ")
