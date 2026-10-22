"""Benchmark harness: strict replay fails closed; receipts detect tampering (Phase 7 exit gate).
Agent tool calls replay from the recording, without running the tool.

Uses validation cases only (never held-out) and scratch ledgers/cassettes under tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opsbridge.agents.adapters import MockModelAdapter
from opsbridge.canonical import digest
from opsbridge.config import REPO_ROOT, get_settings
from opsbridge.evaluation import receipt
from opsbridge.evaluation.bench import BenchEnvironment
from opsbridge.evaluation.cassette import (
    TOOL_CASSETTE,
    RecordingAdapter,
    ReplayAdapter,
    ReplayFailure,
)
from opsbridge.evaluation.egress import loopback_only
from opsbridge.evaluation.manifest import file_digest
from opsbridge.evidence import retrieval
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


def test_tool_calls_replay_from_the_recording_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = subset(["it-validation-107"])  # the mock's one evidence search finds a document
    [scenario] = corpus.scenarios
    rec = RecordingAdapter(inner=MockModelAdapter())
    with loopback_only(), BenchEnvironment(get_settings(), corpus, tmp_path / "w1", rec) as env:
        first = env.run_trial(scenario, "mock", 0)
    assert first.error is None and [c["origin"] for c in first.tool_calls] == ["live"]
    cassette = tmp_path / "cassette.json"
    rec.save(cassette, benchmark_id="test", split="validation")
    tools = tmp_path / TOOL_CASSETTE
    recorded = json.loads(tools.read_text())
    assert recorded["response_mode"] == "live" and len(recorded["entries"]) == 1
    assert recorded["entries"][0]["response"]  # hits to re-bind to the replay database

    def no_search(*_a: object, **_k: object) -> list[object]:
        raise AssertionError("replay executed the search tool")

    def replay(workdir: str, body: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
        if body is not None:
            tools.write_text(json.dumps(body))
        adapter = ReplayAdapter.load(cassette)
        with (
            loopback_only(),
            BenchEnvironment(get_settings(), corpus, tmp_path / workdir, adapter) as env,
        ):
            return env.run_trial(scenario, "mock", 0)

    monkeypatch.setattr(retrieval, "search", no_search)
    again = replay("w2")
    assert again.error is None and verdict(again) == verdict(first)
    # The same response (by digest) is bound to the call, now labelled as recorded.
    assert [{**c, "origin": "live"} for c in again.tool_calls] == first.tool_calls
    assert [c["origin"] for c in again.tool_calls] == ["recorded"]

    def edited(change) -> dict[str, object]:  # type: ignore[no-untyped-def]
        body = json.loads(json.dumps(recorded))
        change(body["entries"][0])
        return body

    missing = {**recorded, "entries": []}
    other_query = edited(lambda e: e.update(args_sha256=digest({"query": "something else"})))
    other_tool = edited(lambda e: e.update(version="2"))

    def foreign(e: dict[str, object]) -> None:  # a hit for a document this case does not have
        e["response"][0]["sha256"] = "sha256:" + "1" * 64  # type: ignore[index]
        e["response_sha256"] = digest(e["response"])

    for name, body, message in (
        ("missing", missing, "ReplayFailure: no recorded tool response"),
        ("query", other_query, "ReplayFailure: tool call diverged from the recording"),
        ("tool", other_tool, "recorded search_case_evidence v2, now search_case_evidence v1"),
        ("foreign", edited(foreign), "does not fit this environment"),
    ):
        t = replay(f"w-{name}", body)
        assert t.error is not None and message in t.error, (name, t.error)
        assert t.explanation_origin is None, name  # no template fallback, no live search
        assert t.tool_calls == [], name  # not recorded as an ordinary tool error either

    # A recorded response that no longer matches its digest is refused before any run.
    tools.write_text(json.dumps(edited(lambda e: e["response"][0].update(text="edited"))))
    with pytest.raises(ReplayFailure, match="does not match its digest"):
        ReplayAdapter.load(cassette)


def test_a_recorded_tool_error_replays_as_the_same_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = subset(["it-validation-107"])
    [scenario] = corpus.scenarios

    def broken(*_a: object, **_k: object) -> list[object]:
        raise RuntimeError("search index unavailable")

    monkeypatch.setattr(retrieval, "search", broken)
    rec = RecordingAdapter(inner=MockModelAdapter())
    with loopback_only(), BenchEnvironment(get_settings(), corpus, tmp_path / "w1", rec) as env:
        first = env.run_trial(scenario, "mock", 0)
    assert first.error is None and [c["outcome"] for c in first.tool_calls] == ["error"]
    rec.save(tmp_path / "cassette.json", benchmark_id="test", split="validation")
    [entry] = json.loads((tmp_path / TOOL_CASSETTE).read_text())["entries"]
    assert entry["error"] == "search index unavailable" and "response" not in entry

    replay = ReplayAdapter.load(tmp_path / "cassette.json")
    with loopback_only(), BenchEnvironment(get_settings(), corpus, tmp_path / "w2", replay) as env:
        again = env.run_trial(scenario, "mock", 0)
    assert again.error is None and verdict(again) == verdict(first)
    assert [(c["outcome"], c["origin"]) for c in again.tool_calls] == [("error", "recorded")]


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


def test_tier_quotas_do_not_throttle_the_benchmark_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held-out replay runs 240 investigations in minutes; tier quotas (60 per hour, 200
    model calls per day) once turned most of them into QuotaExceeded errors and silently
    pushed agent mode back to rules. The harness switches quotas off; prove it with tiny ones."""
    import dataclasses

    from opsbridge.auth import permissions

    for tier in ("standard", "enhanced"):
        monkeypatch.setitem(
            permissions.TIERS,
            tier,
            dataclasses.replace(
                permissions.TIERS[tier], max_investigations_per_hour=1, max_model_calls_per_day=1
            ),
        )
    corpus = subset(IDS[:1])
    with (
        loopback_only(),
        BenchEnvironment(get_settings(), corpus, tmp_path / "w", MockModelAdapter()) as env,
    ):
        trials = [
            env.run_trial(corpus.scenarios[0], mode, r)
            for mode in ("rules_only", "mock")
            for r in range(3)
        ]
    assert [t.error for t in trials] == [None] * 6
    assert all(t.explanation_origin == "model:mock" for t in trials if t.mode == "mock")
