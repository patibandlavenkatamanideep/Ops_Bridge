"""Replay depends on the frozen run order; out of order it fails closed (production review A13).

The v1 recordings were made with `investigation/1.1.0` and are replayed with it (the frozen
plan names no workflow, which means 1.1.0). In that workflow a case's first investigation in
a run ingests its documents and reports their read problems; a later investigation of the
same bytes creates no new version and reports none, so the "could not be read reliably
(...)" finding, and the model request built from it, differ with the order the trials ran
in. The v1 benchmark runs rules-only before mock (its frozen order), and its recordings
replay only in that order. Run in another order, the replay must fail closed (a request that
differs from the recording), never yield a different result quietly. Uses the committed
security recording (not held-out data). `investigation/1.2.0` removed the order dependence
(test_reinvestigation_regression_cases.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opsbridge.config import REPO_ROOT, get_settings
from opsbridge.evaluation import manifest
from opsbridge.evaluation.bench import BenchEnvironment
from opsbridge.evaluation.cassette import ReplayAdapter
from opsbridge.evaluation.egress import loopback_only
from opsbridge.evaluation.reproduce import trial_differences
from opsbridge.simulator.scenarios import load_corpus

pytestmark = pytest.mark.integration
RUN = REPO_ROOT / "evals/runs/opsbridge-bench-v1/security-record-20261015T022617Z"
CORPUS = REPO_ROOT / "fixtures/scenarios/security.yaml"
PLAN = REPO_ROOT / "evals/manifests/opsbridge-bench-v1.json"


def _replay(tmp: Path, modes: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    corpus = load_corpus(CORPUS)
    recorded = {
        m: [json.loads(x) for x in (RUN / f"trials-{m}.jsonl").read_text().splitlines() if x]
        for m in modes
    }
    adapter = ReplayAdapter.load(RUN / "model-cassette.json")
    workflow = manifest.workflow_version_of(manifest.load(PLAN))
    assert workflow == "investigation/1.1.0"
    with (
        loopback_only(),
        BenchEnvironment(get_settings(), corpus, tmp, adapter, workflow_version=workflow) as env,
    ):
        replayed = {
            m: [env.run_trial(s, m, r).to_dict() for s in corpus.scenarios for r in range(3)]
            for m in modes
        }
    return recorded, replayed


def test_the_frozen_order_reproduces_every_trial(tmp_path: Path) -> None:
    recorded, replayed = _replay(tmp_path, ["rules_only", "mock"])
    assert trial_differences(recorded, replayed)[0] == []


@pytest.mark.parametrize(
    "modes", [["mock"], ["mock", "rules_only"]], ids=["mock_alone", "reversed"]
)
def test_another_order_fails_closed_instead_of_differing_quietly(
    tmp_path: Path, modes: list[str]
) -> None:
    recorded, replayed = _replay(tmp_path, modes)
    failed = [t for m in replayed.values() for t in m if t["error"]]
    assert any((t["error"] or "").startswith("ReplayFailure") for t in failed)
    # Every trial that did complete reproduces its recording; the rest failed, visibly.
    done = {m: [t for t in ts if not t["error"]] for m, ts in replayed.items()}
    keys = {m: {(t["case_id"], t["repeat"]) for t in ts} for m, ts in done.items()}
    subset = {m: [t for t in recorded[m] if (t["case_id"], t["repeat"]) in keys[m]] for m in modes}
    assert trial_differences(subset, done)[0] == []
