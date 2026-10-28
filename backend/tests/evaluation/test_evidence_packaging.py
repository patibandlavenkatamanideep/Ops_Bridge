"""What the runtime image can verify about evaluations, and what it says it cannot
(production review A17).

The image carries evaluation results, manifests, the ledger and the public key, but not the
evidence bundle (`evals/runs`: cassettes and bound trial files). Before, `eval-verify` in such
an installation reported every record as FAILED, which reads like tampering. Now it verifies
what is there (ledger chain and signatures), says the artifacts are not in this installation
and where to verify them, and exits 3 (partial) instead of passing; `--summary-only` asks for
exactly that check. Full verification and replay stay with the repository.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from opsbridge import cli, config


@pytest.fixture
def image_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An installation laid out like the runtime image: evals/ without runs/."""
    evals = config.REPO_ROOT / "evals"
    root = tmp_path / "app"
    for part in ("ledger", "keys", "manifests", "results"):
        shutil.copytree(evals / part, root / "evals" / part)
    shutil.copy(evals / "acceptance-policy.json", root / "evals" / "acceptance-policy.json")
    monkeypatch.setattr(config, "REPO_ROOT", root)
    monkeypatch.chdir(tmp_path)  # eval-verify changes directory; restored afterwards
    return root


def test_without_the_evidence_bundle_verification_is_partial_and_says_so(
    image_layout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["eval-verify"]) == 3
    out = capsys.readouterr().out
    assert "chain: " in out and "signatures: " in out
    assert "not in this installation" in out and "FAILED" not in out


def test_summary_only_verifies_the_ledger_it_has(
    image_layout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["eval-verify", "--summary-only"]) == 0
    assert "artifacts: not checked" in capsys.readouterr().out


def test_a_tampered_ledger_still_fails_without_the_bundle(
    image_layout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = image_layout / "evals" / "ledger" / "opsbridge-bench.jsonl"
    lines = ledger.read_text().splitlines()
    assert '"opsbridge-rules_only"' in lines[0]
    lines[0] = lines[0].replace('"opsbridge-rules_only"', '"opsbridge-rules_onlX"', 1)
    ledger.write_text("\n".join(lines) + "\n")
    assert cli.main(["eval-verify", "--summary-only"]) == 1
