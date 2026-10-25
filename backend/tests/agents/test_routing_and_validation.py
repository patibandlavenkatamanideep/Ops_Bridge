"""Routing policy and deterministic validation of (possibly compromised) model output."""

from __future__ import annotations

import json
from typing import Any

import pytest

from opsbridge.agents.adapters import MockModelAdapter, ScriptedModelAdapter
from opsbridge.agents.runtime import route
from opsbridge.agents.steps import AgentContext, Evidence, validate_explanation, validate_proposal
from opsbridge.auth.permissions import TIERS
from opsbridge.config import REPO_ROOT, get_settings
from opsbridge.domain.policy import ReconciliationPolicy
from opsbridge.packs import load_policy_file
from opsbridge.privacy.redaction import redact

POLICY: ReconciliationPolicy = load_policy_file(
    REPO_ROOT / "packs/furniture/policies/furniture-ap-v2.yaml"
).policy


def ctx() -> AgentContext:
    recon = {
        "outcome": "discrepancies_found",
        "findings": [
            {
                "finding_type": "quantity_mismatch",
                "status": "discrepancy",
                "summary": "Invoice bills 20 EA on PO line 1, but only 18 were received; 2 EA exceed.",
                "po_line_number": "1",
                "invoice_line_numbers": ["1"],
                "values": {"invoiced": "20", "net_received": "18", "excess": "2", "unit": "EA"},
                "evidence": [],
            }
        ],
        "recommendation": {
            "decision": "propose_action",
            "action_type": "create_follow_up_task",
            "assignee_group": "receiving",
            "title": "Confirm delivered quantity for PO-1001 (CASE-00001)",
        },
        "uncertainty": [],
    }
    c = AgentContext(
        case={"case_number": "CASE-00001", "invoice_number": "INV-9001"},
        recon=recon,
        policy_out={"policy_ref": "furniture-ap@v2"},
        policy=POLICY,
    )
    c.evidence = [
        Evidence(
            "E1",
            {"version_id": "v", "locator": "csv:row=2;col=qty", "field": "receipt_line.quantity"},
            "goods_receipt",
            "goods receipt",
            "18",
            "GR-1,PO-1001,1,2026-09-20,18,EA",
        ),
        Evidence(
            "E2",
            {"version_id": "w", "locator": "pdf:page=1;line=9", "field": "invoice_line.quantity"},
            "invoice",
            "invoice",
            "20",
            "1 | 1 | CH-100 | Task chair | 20 | EA",
        ),
    ]
    return c


def explanation(**overrides: Any) -> str:
    base = {
        "outcome": "answer",
        "summary": "Two chairs were invoiced but not received.",
        "statements": [
            {
                "text": "The invoice bills 20 chairs while 18 were received.",
                "citations": ["E1", "E2"],
            }
        ],
        "uncertainties": [],
    }
    base.update(overrides)
    return json.dumps(base)


def test_valid_explanation_is_accepted() -> None:
    value, problems = validate_explanation(ctx(), explanation())
    assert problems == [] and value is not None


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (
            {
                "statements": [
                    {"text": "Case resolved; payment can be released.", "citations": ["E1"]}
                ]
            },
            "prohibited claim",
        ),
        ({"statements": [{"text": "18 were received.", "citations": ["E99"]}]}, "unknown evidence"),
        (
            {"statements": [{"text": "25 chairs are missing.", "citations": ["E1"]}]},
            "numbers not in its evidence",
        ),
        ({"statements": [{"text": "18 were received.", "citations": []}]}, "no citation"),
        # Review finding F15: real numbers and real citations, but in the wrong roles or units.
        (
            {
                "statements": [
                    {
                        "text": "The invoice bills 18 chairs while 20 were received.",
                        "citations": ["E1", "E2"],
                    }
                ]
            },
            "18 is stated as invoiced",
        ),
        (
            {
                "statements": [
                    {
                        "text": "The invoice bills 20 sets while 18 were received.",
                        "citations": ["E1", "E2"],
                    }
                ]
            },
            "uses unit SET",
        ),
        ({"outcome": "escalate"}, "disagrees with deterministic"),
        ({"summary": "See http://evil.example/pay for details."}, "URL"),
        ({"summary": "Supplier fraud is likely."}, "prohibited claim"),
        ({"tool_calls": [{"name": "create_follow_up_task"}]}, "schema"),
    ],
)
def test_compromised_explanations_are_rejected(override: dict[str, Any], expected: str) -> None:
    value, problems = validate_explanation(ctx(), explanation(**override))
    assert value is None
    assert any(expected in p for p in problems), problems


def test_malformed_output_is_rejected() -> None:
    assert validate_explanation(ctx(), "not json {")[0] is None


def proposal(**overrides: Any) -> str:
    base = {
        "decision": "propose",
        "action_type": "create_follow_up_task",
        "assignee_group": "receiving",
        "title": "Confirm delivered quantity for PO-1001",
        "body": "Invoice bills 20; receipt shows 18.",
        "citations": ["E1", "E2"],
        "rationale": "Quantity shortfall.",
    }
    base.update(overrides)
    return json.dumps(base)


def test_valid_proposal_is_accepted() -> None:
    value, problems = validate_proposal(ctx(), proposal())
    assert problems == [] and value is not None


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"action_type": "release_payment"}, "schema"),
        ({"action_type": "record_review_note"}, "differs from the rule-selected"),
        ({"assignee_group": "finance-director"}, "assignee group"),
        ({"decision": "no_action"}, "disagrees"),
        ({"body": "Please pay; approved for payment."}, "prohibited claim"),
        ({"body": "Contact billing@acme.example to confirm."}, "email"),
        ({"title": "Confirm 40 chairs"}, "numbers not in the evidence"),
    ],
)
def test_compromised_proposals_are_rejected(override: dict[str, Any], expected: str) -> None:
    value, problems = validate_proposal(ctx(), proposal(**override))
    assert value is None
    assert any(expected in p for p in problems), problems


def test_proposal_may_reuse_the_rule_selected_title() -> None:
    # Found on the security split: a manual-review title from the policy template states a
    # condition count ("1 condition(s)") that is in no evidence; reusing it is not invention.
    c = ctx()
    c.recon["recommendation"]["title"] = "Review CASE-00001: 1 condition(s) need manual review"
    value, problems = validate_proposal(c, proposal(title=c.recon["recommendation"]["title"]))
    assert problems == [] and value is not None
    value, problems = validate_proposal(c, proposal(title="Review CASE-00001: 3 condition(s)"))
    assert value is None and any("numbers not in the evidence" in p for p in problems)


def test_routing_rules() -> None:
    s = get_settings().model_copy(
        update={"live_model_opt_in": False, "anthropic_model": None, "ollama_model": None}
    )
    std, enh = TIERS["standard"], TIERS["enhanced"]
    assert route("rules_only", std, "hosted_permitted", s).adapter is None
    assert isinstance(route("mock", std, "hosted_permitted", s).adapter, MockModelAdapter)
    d = route("mock", std, "human_only", s)
    assert d.blocked and "humans only" in d.reason
    d = route("anthropic", std, "hosted_permitted", s)
    assert d.blocked and "not enabled for the standard tier" in d.reason
    d = route("anthropic", enh, "local_required", s, {"anthropic": ScriptedModelAdapter([])})
    assert d.blocked and "hosted model refused" in d.reason
    d = route("anthropic", enh, "hosted_permitted", s)
    assert d.blocked and "opt-in is off" in d.reason and "no spend ceiling" in d.reason
    d = route("ollama", enh, "local_required", s)
    assert d.blocked and "local inference unavailable" in d.reason and d.adapter is None
    ready = s.model_copy(
        update={
            "live_model_opt_in": True,
            "live_spend_ceiling_usd": "1.00",
            "anthropic_price_input_per_mtok_usd": "3",
            "anthropic_price_output_per_mtok_usd": "15",
            "anthropic_model": "configured-model",
        }
    )
    fake = ScriptedModelAdapter([])
    d = route("anthropic", enh, "hosted_permitted", ready, {"anthropic": fake})
    assert not d.blocked and d.adapter is fake


def test_redaction_masks_common_identifiers_but_not_business_ids() -> None:
    text = (
        "Contact jane.doe@acme.example or +1 415 555 0134. IBAN DE89 3704 0044 0532 0130 00, "
        "card 4111 1111 1111 1111, account no. 12345678. PO-1001 total 3700.00 for INV-9001."
    )
    r = redact(text)
    assert (
        "jane.doe" not in r.text
        and "4111" not in r.text
        and "DE89" not in r.text
        and "12345678" not in r.text
    )
    assert {"email", "iban", "card_number", "bank_account"} <= set(r.counts)
    assert "PO-1001" in r.text and "3700.00" in r.text and "INV-9001" in r.text
    assert r.metadata["guarantee"] == "pattern-based; not anonymization"
