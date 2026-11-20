"""The synthetic receiving and disposition pack (fixtures/receiving/disposition.yaml), read
through the furniture pack mapping and the current rules, against labels written by hand from
furniture-ap@v2 before the rules were run. Regression evidence on synthetic data only."""

from __future__ import annotations

import pytest

from opsbridge.config import REPO_ROOT
from opsbridge.domain.decision import recommend
from opsbridge.domain.matching import reconcile
from opsbridge.evaluation.rules_baseline import PolicyBook, build_input
from opsbridge.pilot.receiving_pack import Case, load_pack, render_case
from opsbridge.simulator.scenarios import load_corpus

BOOK = PolicyBook(REPO_ROOT / "packs")
PACK = load_pack()


@pytest.mark.parametrize("case", PACK.cases, ids=lambda c: c.id)
def test_each_case_meets_its_hand_written_label(case: Case) -> None:
    pack = BOOK.packs.get("furniture")
    data, problems, _ = build_input(case.scenario, pack, render_case(case))
    assert problems == [] and data is not None, problems
    policy = BOOK.effective(pack.policy_id, case.scenario.invoice.date)
    assert policy is not None and policy.ref == "furniture-ap@v2"
    result = reconcile(data, policy)
    rec = recommend(result, policy, case.id)
    assert (result.outcome, rec.label, rec.assignee_group) == (
        case.expected.outcome,
        case.expected.next_action,
        case.expected.owner,
    )
    assert sorted(f.reason_code for f in result.findings) == case.expected.codes


def test_the_pack_covers_each_receiving_fact_and_remedy() -> None:
    events = {ev.event for c in PACK.cases for ev in c.events}
    assert events == {
        "REJECTED",
        "RETURNED",
        "REPLACEMENT_PROMISED",
        "REPLACEMENT_DELIVERED",
        "REPLACEMENT_ACCEPTED",
        "CREDIT_PROMISED",
        "CREDIT_POSTED",
    }
    statuses = {ln.status for c in PACK.cases for r in c.receipts for ln in r.lines}
    assert statuses == {"ACCEPTED", "REJECTED"}


def test_no_order_invoice_or_receipt_number_is_shared_with_another_corpus() -> None:
    """The pack is seeded beside the dev corpus and the rehearsal cases in one organization."""
    ours = {
        n
        for c in PACK.cases
        for n in (c.po_number, c.scenario.invoice.number, *(r.id for r in c.receipts))
    }
    others = set()
    for path in (
        REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml",
        REPO_ROOT / "fixtures" / "pilot" / "rehearsal.yaml",
    ):
        for s in load_corpus(path).scenarios:
            others |= {s.invoice.number, *(r.id for r in s.receipts)}
            if s.po is not None:
                others.add(s.po.number)
    assert not ours & others


def test_the_simulator_gets_the_late_rejection_only_when_asked(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from opsbridge.pilot.receiving_pack import ORG, seed_simulator
    from opsbridge.simulator.store import SimStore

    store = SimStore(tmp_path / "sim.sqlite3")
    early = seed_simulator(store, PACK)
    assert store.get_record(ORG, "receiving_events", "PO-4609-EVT-1") is None
    assert store.get_record(ORG, "receipts", "GR-6609") is not None
    assert seed_simulator(store, PACK, late=True) == 1
    assert store.get_record(ORG, "receiving_events", "PO-4609-EVT-1") is not None
    assert early == sum(len(render_case(c)) for c in PACK.cases) - 1
