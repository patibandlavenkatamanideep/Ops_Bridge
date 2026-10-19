"""Benchmark harness: strict replay fails closed; receipts detect tampering (Phase 7 exit gate).

Uses validation cases only (never held-out) and scratch ledgers/cassettes under tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opsbridge.agents.adapters import MockModelAdapter
from opsbridge.config import REPO_ROOT, get_settings
from opsbridge.evaluation import receipt
from opsbridge.evaluation.bench import BenchEnvironment
from opsbridge.evaluation.cassette import RecordingAdapter, ReplayAdapter, ReplayFailure
from opsbridge.evaluation.egress import loopback_only
from opsbridge.evaluation.manifest import file_digest
from opsbridge.simulator.scenarios import Corpus, load_corpus

pytestmark = pytest.mark.integration
VALIDATION = REPO_ROOT / "fixtures" / "scenarios" / "validation.yaml"
# A quantity case, a furniture case with a supplier message (the mock requests an evidence
# search), and an IT conflict that escalates.
IDS = ["fur-validation-101", "fur-validation-111", "it-validation-107"]


def subset(ids: list[str]) -> Corpus:
    c = load_corpus(VALIDATION)
    return Corpus(corpus_id=c.corpus_id, scenarios=[s for s in c.scenarios if s.id in ids])


def verdict(t):  # type: ignore[no-untyped-def]
    return (t.case_id, t.outcome, t.types, t.next_action, t.citations_valid, len(t.tool_calls))


def test_strict_replay_reproduces_recorded_runs_and_fails_closed(tmp_path: Path) -> None:
    corpus = subset(IDS)
    rec = RecordingAdapter(inner=MockModelAdapter())
    with loopback_only(), BenchEnvironment(get_settings(), corpus, tmp_path / "w1", rec) as env:
        first = [env.run_trial(s, "mock", 0) for s in corpus.scenarios]
    assert all(t.error is None and t.run_status == "completed" for t in first), [
        t.error for t in first
    ]
    assert all(t.explanation_origin == "model:mock" and t.response_mode == "mocked" for t in first)
    assert any(t.tool_calls for t in first)  # the supplier-message case searched evidence
    cassette = tmp_path / "cassette.json"
    rec.save(cassette, benchmark_id="test", split="validation")

    replay = ReplayAdapter.load(cassette)
    with loopback_only(), BenchEnvironment(get_settings(), corpus, tmp_path / "w2", replay) as env:
        again = [env.run_trial(s, "mock", 0) for s in corpus.scenarios]
    assert [verdict(t) for t in again] == [verdict(t) for t in first]
    assert all(
        t.explanation_origin == "model:replay" and t.response_mode == "mocked" for t in again
    )

    # A missing recording fails the trial explicitly; nothing falls back to a template or a live call.
    body = json.loads(cassette.read_text())
    target = corpus.scenarios[0].id
    body["entries"] = [e for e in body["entries"] if not (e["case_id"] == target and e["seq"] == 1)]
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps(body))
    with (
        loopback_only(),
        BenchEnvironment(
            get_settings(), corpus, tmp_path / "w3", ReplayAdapter.load(missing)
        ) as env,
    ):
        t = env.run_trial(corpus.scenarios[0], "mock", 0)
    assert t.error is not None and t.error.startswith("ReplayFailure: no recorded response")
    assert t.explanation_origin is None  # no investigation was produced from a fallback

    # A request that differs from the recording (divergent path) also fails.
    body = json.loads(cassette.read_text())
    for e in body["entries"]:
        if e["case_id"] == target and e["seq"] == 1:
            e["request_sha256"] = "sha256:" + "0" * 64
    diverged = tmp_path / "diverged.json"
    diverged.write_text(json.dumps(body))
    with (
        loopback_only(),
        BenchEnvironment(
            get_settings(), corpus, tmp_path / "w4", ReplayAdapter.load(diverged)
        ) as env,
    ):
        t = env.run_trial(corpus.scenarios[0], "mock", 0)
    assert t.error is not None and "diverged from the recording" in t.error


def _run(
    env: BenchEnvironment, corpus: Corpus, adapter, tmp: Path, mode: str, bound: dict[str, str]
):  # type: ignore[no-untyped-def]
    return receipt.run_mode(
        env,
        corpus.scenarios,
        mode=mode,
        repeats=2,
        split="validation",
        corpus_rel=str(VALIDATION),
        corpus_sha=file_digest(VALIDATION),
        run_dir=tmp / "run",
        ledger=tmp / "ledger.jsonl",
        key_path=tmp / "eval.key",
        bound=bound,
        adapter=adapter,
        cassette_rel=str(tmp / "cassette.json"),
        benchmark_id="test",
    )


def test_receipt_detects_edited_and_missing_artifacts_and_replay_miss_seals_nothing(
    tmp_path: Path,
) -> None:
    corpus = subset(IDS[:2])
    public = receipt.ensure_key(tmp_path / "eval.key", tmp_path / "eval.pub")
    policy = tmp_path / "policy.yaml"
    policy.write_bytes((REPO_ROOT / "packs/furniture/policies/furniture-ap-v2.yaml").read_bytes())
    rec = RecordingAdapter(inner=MockModelAdapter())
    with loopback_only(), BenchEnvironment(get_settings(), corpus, tmp_path / "w", rec) as env:
        _run(env, corpus, rec, tmp_path, "rules_only", {})  # two records chain in one ledger
        trials, record = _run(env, corpus, rec, tmp_path, "mock", {"opsbridge_policy": str(policy)})
    assert len(trials) == 4 and [r.verdicts for r in record.results] == [[1, 1], [1, 1]]
    roles = {a.role for a in record.manifest.artifacts}
    assert {
        "tool_acl",
        "tool_responses",
        "opsbridge_trials",
        "opsbridge_model_cassette",
        "opsbridge_policy",
    } <= roles
    assert receipt.verify(tmp_path / "ledger.jsonl", public)["ok"]

    log = tmp_path / "run" / "trials-mock.jsonl"
    original = log.read_text()
    log.write_text(
        original.replace('"next_action": "propose_action', '"next_action": "no_discrepancy', 1)
    )
    report = receipt.verify(tmp_path / "ledger.jsonl", public)
    assert not report["ok"] and "changed" in report["artifacts"][1]["output"]
    log.write_text(original)
    policy.unlink()
    report = receipt.verify(tmp_path / "ledger.jsonl", public)
    assert not report["ok"] and "missing" in report["artifacts"][1]["output"]
    assert report["artifacts"][0]["ok"]  # the rules-only record bound neither file

    # A replay with a missing response aborts the EvalSeal run: no new record is sealed.
    body = json.loads((tmp_path / "cassette.json").read_text())
    body["entries"] = body["entries"][1:]
    (tmp_path / "cassette.json").write_text(json.dumps(body))
    before = (tmp_path / "ledger.jsonl").read_text()
    replay = ReplayAdapter.load(tmp_path / "cassette.json")
    with loopback_only(), BenchEnvironment(get_settings(), corpus, tmp_path / "w2", replay) as env:
        with pytest.raises(ReplayFailure):
            _run(env, corpus, replay, tmp_path, "mock", {})
    assert (tmp_path / "ledger.jsonl").read_text() == before


def test_evaluation_api_shows_own_pack_cases_and_validates_run_ids(stack) -> None:  # type: ignore[no-untyped-def]
    from .stack import get

    st = stack
    runs = get(st, "furn-investigator", "/api/v1/tenants/demo-furniture/evaluations").json()[
        "items"
    ]
    held = next(r for r in runs if r["split"] == "heldout" and r["kind"] == "record")
    url = f"/api/v1/tenants/demo-furniture/evaluations/{held['benchmark_id']}/{held['run_id']}"
    detail = get(st, "furn-investigator", url).json()
    # every committed run was graded under the current frozen plan and scoring
    assert all(r["comparability"]["comparable"] for r in runs)
    assert detail["comparability"] == {"comparable": True, "reasons": [], "note": None}
    cases = detail["modes"]["rules_only"]["pack_cases"]
    assert cases and all(c["case_id"].startswith("fur-") for c in cases)  # no IT-pack case detail
    assert detail["modes"]["rules_only"]["n_cases"] == 40  # aggregates cover the whole benchmark
    it = get(st, "it-investigator", url.replace("demo-furniture", "demo-it")).json()
    assert all(c["case_id"].startswith("it-") for c in it["modes"]["rules_only"]["pack_cases"])
    receipt_resp = get(st, "furn-investigator", url + "/receipt")
    assert receipt_resp.headers["content-disposition"].startswith("attachment;")
    assert {r["hash"] for r in receipt_resp.json()} == {
        r["record_hash"] for r in detail["receipts"].values()
    }
    for bad in ("../../../etc/passwd", "heldout-record-x", "..%2F..%2Fmanifests"):
        assert (
            get(
                st,
                "furn-investigator",
                f"/api/v1/tenants/demo-furniture/evaluations/{held['benchmark_id']}/{bad}",
            ).status_code
            == 404
        )
    assert (
        get(st, "furn-requester", "/api/v1/tenants/demo-furniture/evaluations").status_code == 403
    )
