"""Regression evidence (not evaluation evidence): with `investigation/1.2.0` the invoice
finding no longer depends on which investigation of a case ran first (production review A13).

Reuses two cases of the public opsbridge-bench-v1 security split whose recordings exposed the
defect: fur-security-303 (an image-only invoice) and fur-security-304 (a line amount that
disagrees with quantity x price). These cases have been public and replayed many times, so
passing here shows the defect is fixed; it is not a measurement of the benchmark, and these
trials are neither recorded nor scored. `investigation/1.1.0` is run alongside to show the
version selects the behaviour. It named only problems found by the current run's read: a
problem found when the record was mapped (304) on the case's first investigation only, which
is why the v1 recordings replay only in their frozen order (test_replay_mode_order.py), and
a problem found by the parser (303: no text layer) never.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opsbridge.config import REPO_ROOT, get_settings
from opsbridge.evaluation.bench import BenchEnvironment
from opsbridge.evaluation.egress import loopback_only
from opsbridge.services.investigations import latest_investigation
from opsbridge.simulator.scenarios import load_corpus
from opsbridge.workflow.investigation import LEGACY_WORKFLOW_VERSION, WORKFLOW_VERSION

pytestmark = pytest.mark.integration
CORPUS = REPO_ROOT / "fixtures/scenarios/security.yaml"
CASES = ("fur-security-303", "fur-security-304")
REASONS = {
    "fur-security-303": "invoice INV-9303: no extractable text layer",
    "fur-security-304": "invoice INV-9304: invoice: line amount 2210.00 != 10 x 220.00",
}
REASON = "invoice_extraction_needs_review"


def _summaries(tmp: Path, workflow_version: str, modes: list[str]) -> dict[str, list[str]]:
    """The invoice finding's summary after each trial, per case, in the order run."""
    corpus = load_corpus(CORPUS)
    scenarios = [s for s in corpus.scenarios if s.id in CASES]
    seen: dict[str, list[str]] = {c: [] for c in CASES}
    with (
        loopback_only(),
        BenchEnvironment(
            get_settings(), corpus, tmp, None, workflow_version=workflow_version
        ) as env,
    ):
        for mode in modes:
            for s in scenarios:
                for repeat in range(2):
                    trial = env.run_trial(s, mode, repeat)
                    assert trial.error is None, trial.error
                    view = latest_investigation(env.app_db, env.principals[s.pack], env.cases[s.id])
                    assert view is not None
                    found = [f for f in view.version.findings if f["reason_code"] == REASON]
                    assert len(found) == 1, view.version.findings
                    seen[s.id].append(found[0]["summary"])
    return seen


@pytest.mark.parametrize(
    "modes", [["rules_only", "mock"], ["mock", "rules_only"]], ids=["frozen_order", "reversed"]
)
def test_every_investigation_names_the_reason_in_any_order(
    tmp_path: Path, modes: list[str]
) -> None:
    for case, summaries in _summaries(tmp_path, WORKFLOW_VERSION, modes).items():
        assert len(set(summaries)) == 1, (case, summaries)  # first, repeats and other mode
        assert f"({REASONS[case]}" in summaries[0], (case, summaries[0])


def test_the_recorded_workflow_named_only_what_the_current_read_found(tmp_path: Path) -> None:
    summaries = _summaries(tmp_path, LEGACY_WORKFLOW_VERSION, ["rules_only"])
    first, *repeats = summaries["fur-security-304"]  # found when the record was mapped
    assert f"({REASONS['fur-security-304']}" in first
    assert repeats and all("reliably ()" in s for s in repeats), repeats
    assert all("reliably ()" in s for s in summaries["fur-security-303"])  # found by the parser
