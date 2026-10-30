"""A frozen plan names the acceptance policy its runs are judged by, and what it is for.

`opsbridge-bench-v1-rerun` re-runs the public v1 cases with the current code (scoring 2,
`investigation/1.2.0`) as regression evidence. Judged by v1's policy, its held-out split would
show a pass or fail on cases that are not held out (public, replayed since 2026-10-15), so it
has its own policy, which reports that gate as not evaluated and says why. v1 keeps the
policy it was frozen with (a plan without the field) and verifies unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opsbridge.canonical import digest
from opsbridge.config import REPO_ROOT
from opsbridge.evaluation import manifest, metrics
from opsbridge.services import evaluations

V1 = REPO_ROOT / "evals" / "manifests" / "opsbridge-bench-v1.json"
RERUN_POLICY = "evals/acceptance-policies/opsbridge-bench-v1-rerun.json"
PURPOSE = "regression re-run of public cases"


def _rerun_plan() -> dict[str, Any]:
    return manifest.freeze(
        REPO_ROOT,
        "opsbridge-bench-v1-rerun",
        modes=["rules_only", "mock"],
        repeats=3,
        acceptance_policy=RERUN_POLICY,
        purpose=PURPOSE,
    )


def test_v1_keeps_the_policy_it_was_frozen_with() -> None:
    frozen = manifest.load(V1)
    assert "acceptance_policy" not in frozen and "purpose" not in frozen
    assert manifest.acceptance_policy_of(frozen) == "evals/acceptance-policy.json"
    assert manifest.verify(frozen, REPO_ROOT) == []
    assert (
        manifest.load_acceptance_policy(frozen, REPO_ROOT)["benchmark_id"] == "opsbridge-bench-v1"
    )


def test_a_plan_pins_its_own_policy_and_purpose() -> None:
    plan = _rerun_plan()
    assert plan["acceptance_policy"] == RERUN_POLICY and plan["purpose"] == PURPOSE
    assert RERUN_POLICY in plan["files"] and "evals/acceptance-policy.json" not in plan["files"]
    assert manifest.verify(plan, REPO_ROOT) == []
    changed = {k: v for k, v in plan.items() if k != "manifest_sha256"}
    changed["files"] = {**plan["files"], RERUN_POLICY: "sha256:" + "0" * 64}
    changed["manifest_sha256"] = digest(changed)
    assert manifest.verify(changed, REPO_ROOT) == [f"{RERUN_POLICY} changed since freezing"]


def test_a_plan_is_never_judged_by_another_benchmarks_policy() -> None:
    borrowed = {**_rerun_plan(), "acceptance_policy": "evals/acceptance-policy.json"}
    with pytest.raises(ValueError, match="opsbridge-bench-v1"):
        manifest.load_acceptance_policy(borrowed, REPO_ROOT)


def _heldout_summary() -> dict[str, Any]:
    rate = {"correct": 36, "n": 40, "rate": 0.9, "wilson95": [0.77, 0.96]}
    mode = {"n_cases": 40, "next_action": rate}
    return {"modes": {"rules_only": mode, "mock": mode}}


def test_the_rerun_reports_the_heldout_gate_as_not_evaluated() -> None:
    policy = manifest.load_acceptance_policy(_rerun_plan(), REPO_ROOT)
    gates = metrics.acceptance(_heldout_summary(), policy, "heldout")
    assert [g["status"] for g in gates] == ["not_evaluated", "not_evaluated"]
    assert all("not held out" in g["reason"] for g in gates)
    # v1's own policy still decides it.
    v1 = manifest.load_acceptance_policy(manifest.load(V1), REPO_ROOT)
    assert [g["status"] for g in metrics.acceptance(_heldout_summary(), v1, "heldout")] == [
        "pass",
        "pass",
    ]


def test_every_frozen_plan_is_replayed() -> None:
    assert "opsbridge-bench-v1" in manifest.frozen_plans(REPO_ROOT)


def test_a_run_page_names_its_purpose_and_workflow(tmp_path: Any) -> None:
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "opsbridge-bench-v1.json").write_text(V1.read_text())
    plan = _rerun_plan()
    (tmp_path / "manifests" / "opsbridge-bench-v1-rerun.json").write_text(
        evaluations.json.dumps(plan)
    )
    viewer = SimpleNamespace(has=lambda permission: True, tenant_pack="furniture")
    for benchmark, purpose, workflow in (
        ("opsbridge-bench-v1", None, "investigation/1.1.0"),
        ("opsbridge-bench-v1-rerun", PURPOSE, "investigation/1.2.0"),
    ):
        run_id = "heldout-record-20261029T000000Z"
        result = {
            "schema": "opsbridge-eval-results/1",
            "benchmark_id": benchmark,
            "run_id": run_id,
            "split": "heldout",
            "kind": "record",
            "repeats": 3,
            "manifest_sha256": "sha256:x",
            "response_origin": "mocked",
            "network": "loopback",
            "code_version": {"commit": None},
            "modes": {},
        }
        (tmp_path / "results" / benchmark).mkdir(parents=True)
        (tmp_path / "results" / benchmark / f"{run_id}.json").write_text(
            evaluations.json.dumps(result)
        )
        detail = evaluations.run_detail(tmp_path, viewer, benchmark, run_id)  # type: ignore[arg-type]
        assert (detail["purpose"], detail["workflow_version"]) == (purpose, workflow)
        [listed] = [
            r
            for r in evaluations.list_runs(tmp_path, viewer)
            if r["benchmark_id"] == benchmark  # type: ignore[arg-type]
        ]
        assert listed["purpose"] == purpose
