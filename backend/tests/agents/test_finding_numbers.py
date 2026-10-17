"""Every number a finding states must be supported by that finding's own values.

The numeric-support validator only lets model text use numbers present in findings and cited
evidence, so a finding whose summary states an unrecorded number makes a faithful restatement
fail validation. Found on the validation split (INV-9105, INV-9107); checked here over the dev
and validation corpora only — held-out data is never used outside the frozen benchmark run.
"""

from __future__ import annotations

import pytest

from opsbridge.agents.steps import AgentContext, _allowed_numbers, _numbers
from opsbridge.config import REPO_ROOT
from opsbridge.domain.matching import reconcile
from opsbridge.evaluation.rules_baseline import PolicyBook, build_input
from opsbridge.simulator.scenarios import load_corpus

BOOK = PolicyBook(REPO_ROOT / "packs")
SCENARIOS = [
    s
    for split in ("dev", "validation")
    for s in load_corpus(REPO_ROOT / "fixtures" / "scenarios" / f"{split}.yaml").scenarios
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_finding_summaries_only_state_recorded_numbers(scenario) -> None:  # type: ignore[no-untyped-def]
    pack = BOOK.packs.get(scenario.pack)
    match_input, _, _ = build_input(scenario, pack)
    policy = BOOK.effective(pack.policy_id, scenario.invoice.date)
    if match_input is None or policy is None:
        pytest.skip("no reconcilable input in this scenario (handled before matching)")
    result = reconcile(match_input, policy)
    for f in result.findings:
        d = f.to_dict()
        ctx = AgentContext(
            case={},
            recon={"findings": [d]},
            policy_out={"policy_ref": result.policy_ref},
            policy=policy,
        )
        unsupported = _numbers(d["summary"]) - _allowed_numbers(ctx, [])
        assert not unsupported, f"{d['reason_code']}: {d['summary']} states {sorted(unsupported)}"
