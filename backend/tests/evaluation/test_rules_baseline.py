"""Rules-only baseline on the hand-labelled development corpus (development verification only)."""

from __future__ import annotations

import pytest

from opsbridge.config import REPO_ROOT
from opsbridge.evaluation.rules_baseline import PolicyBook, evaluate
from opsbridge.simulator.scenarios import load_corpus

BOOK = PolicyBook(REPO_ROOT / "packs")
CORPUS = load_corpus(REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml")


@pytest.mark.parametrize("scenario", CORPUS.scenarios, ids=lambda s: s.id)
def test_dev_scenario_matches_hand_written_labels(scenario) -> None:  # type: ignore[no-untyped-def]
    v = evaluate(scenario, BOOK)
    assert (v.outcome, sorted(v.types), v.next_action) == (
        scenario.expected.outcome,
        sorted(scenario.expected.types),
        scenario.expected.next_action,
    ), v.to_dict()


def test_corpus_covers_required_types_and_outcomes() -> None:
    outcomes = {s.expected.outcome for s in CORPUS.scenarios}
    types = {t for s in CORPUS.scenarios for t in s.expected.types}
    assert outcomes == {"matched", "discrepancies_found", "needs_information", "manual_review"}
    assert types == {"missing_receipt", "quantity_mismatch", "price_mismatch"}
    assert len(CORPUS.scenarios) >= 30
    assert {s.pack for s in CORPUS.scenarios} == {"furniture", "it_hardware"}
