"""EvalSeal 2.2.0 receipts for benchmark runs (documented API only; see ADR 0007).

EvalSeal executes the repeats: each case is an EvalSeal `Case` whose prompt is the case
reference and whose expected value is the hand-written next-action label; the target is a
`LocalCallableTarget` that runs that case through OpsBridge and returns the next action it
reached. The scorer is `ExactMatchScorer`. Files the verdicts depend on are bound by digest
(`artifact_paths`), including OpsBridge-produced `tool_acl` / `tool_responses` files in
EvalSeal's documented agent schemas; other files use opaque `opsbridge_*` roles. Records are
sealed into a hash-chained ledger and signed with a local Ed25519 key.

What this proves: the bound files are unchanged since sealing, relative to the signing key.
It does not prove when the run happened, that the inputs were true, or that no other runs
were made. EvalSeal binds artifacts; it does not run or replay the agent itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evalseal import (
    Case,
    Dataset,
    ExactMatchScorer,
    LocalCallableTarget,
    RunRecord,
    generate_keypair,
    last_hash,
    run_eval,
    seal_and_append,
    sign_head,
    verify_chain,
    verify_signatures,
)

from opsbridge.agents.steps import SEARCH_TOOL, TOOL_VERSION
from opsbridge.canonical import digest
from opsbridge.simulator.scenarios import Scenario

from .bench import BenchEnvironment
from .cassette import RecordingAdapter, ReplayFailure

TOOL_DEFINITIONS = [
    {
        "name": SEARCH_TOOL,
        "version": TOOL_VERSION,
        "description": "Read-only lexical search over the case's authorized evidence chunks.",
        "access": "read",
    }
]
# Write-capable tools exist (Casework MCP) but are never available to investigation runs.
DENIED_TOOLS = ["create_follow_up_task", "get_follow_up_task", "reconcile_operation"]


def tool_acl() -> dict[str, Any]:
    return {
        "schema": "evalseal.agent-tools/1",
        "response_mode": "live",
        "tools": TOOL_DEFINITIONS,
        "allowed": [{"name": t["name"], "definition_sha256": digest(t)} for t in TOOL_DEFINITIONS],
        "denied": DENIED_TOOLS,
        "note": "live = executed during the run against the local, frozen synthetic evidence store",
    }


def tool_responses(trials: list[dict[str, Any]], acl_sha: str) -> dict[str, Any]:
    calls = [
        {
            "case_id": t["case_id"],
            "repeat": t["repeat"],
            "seq": c["seq"],
            "tool": c["tool"],
            "input_sha256": c["input_sha256"],
            "outcome": "ok"
            if c["outcome"] == "ok"
            else ("denied" if c["outcome"] == "denied" else "error"),
            **({"response_sha256": c["response_sha256"]} if c["outcome"] == "ok" else {}),
        }
        for t in trials
        for c in t["tool_calls"]
    ]
    return {
        "schema": "evalseal.agent-tool-responses/1",
        "response_mode": "live",
        "manifest_sha256": acl_sha,
        "calls": calls,
    }


def ensure_key(private_path: Path, public_path: Path) -> str:
    if private_path.exists():
        return public_path.read_text(encoding="utf-8").strip()
    return generate_keypair(private_path, public_path)


def run_mode(
    env: BenchEnvironment,
    scenarios: list[Scenario],
    *,
    mode: str,
    repeats: int,
    split: str,
    corpus_rel: str,
    corpus_sha: str,
    run_dir: Path,
    ledger: Path,
    key_path: Path,
    bound: dict[str, str],
    adapter: Any,
    cassette_rel: str | None,
    benchmark_id: str,
    on_trial: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], RunRecord]:
    """Run one mode on one split under EvalSeal and seal the record. Paths are relative to
    the repository root (the current directory), so receipts carry no machine paths."""
    trials: list[dict[str, Any]] = []
    counters: dict[str, int] = defaultdict(int)
    run_dir.mkdir(parents=True, exist_ok=True)
    trial_log = run_dir / f"trials-{mode}.jsonl"
    trial_log.write_text("", encoding="utf-8")
    acl_path = run_dir / "tool-acl.json"
    acl_path.write_text(json.dumps(tool_acl(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    acl_sha = digest(tool_acl())
    responses_path = run_dir / f"tool-responses-{mode}.json"
    prompts = {
        s.id: json.dumps(
            {
                "benchmark": benchmark_id,
                "case_id": s.id,
                "split": split,
                "corpus_sha256": corpus_sha,
            },
            sort_keys=True,
        )
        for s in scenarios
    }
    by_prompt = {prompts[s.id]: s for s in scenarios}

    def target(prompt: str) -> str:
        s = by_prompt[prompt]
        repeat = counters[s.id]
        counters[s.id] += 1
        result = env.run_trial(s, mode, repeat).to_dict()
        trials.append(result)
        with trial_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, sort_keys=True) + "\n")
        responses_path.write_text(
            json.dumps(tool_responses(trials, acl_sha), indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if isinstance(adapter, RecordingAdapter) and cassette_rel:
            adapter.save(Path(cassette_rel), benchmark_id=benchmark_id, split=split)
        if on_trial:
            on_trial(result)
        if result["error"] and result["error"].startswith("ReplayFailure"):
            raise ReplayFailure(result["error"])  # fail closed: abort; nothing is sealed
        return str(result["next_action"] or "error")

    dataset = Dataset(
        cases=[Case(s.id, prompts[s.id], s.expected.next_action) for s in scenarios],
        hash=corpus_sha,
    )
    artifacts = {
        **bound,
        "opsbridge_trials": str(trial_log),
        "tool_acl": str(acl_path),
        "tool_responses": str(responses_path),
    }
    if mode != "rules_only" and cassette_rel:
        artifacts["opsbridge_model_cassette"] = cassette_rel
    if isinstance(adapter, RecordingAdapter) and cassette_rel and mode != "rules_only":
        Path(cassette_rel).parent.mkdir(parents=True, exist_ok=True)
    record = run_eval(
        dataset,
        LocalCallableTarget(target, name=f"opsbridge-{mode}"),
        ExactMatchScorer(),
        n_repeats=repeats,
        prev_hash=last_hash(ledger) if ledger.exists() else "GENESIS",
        concurrency=1,
        dataset_path=corpus_rel,
        artifact_paths=artifacts,
    )
    sealed = seal_and_append(record, ledger)
    sign_head(ledger, key_path)
    return trials, sealed


def verify(ledger: Path, public_key: str) -> dict[str, Any]:
    """Chain + signatures through the public API; artifacts through the documented CLI
    (`evalseal verify --ledger … --artifacts --index i`) for every record."""
    chain_ok, chain_msg = verify_chain(ledger)
    sig_ok, sig_msg = verify_signatures(ledger, expected_public_key=public_key)
    beside = Path(sys.executable).parent / "evalseal"
    exe = str(beside) if beside.exists() else shutil.which("evalseal")
    records: list[dict[str, Any]] = []
    n = sum(1 for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip())
    for i in range(n):
        if exe is None:
            records.append({"index": i, "ok": False, "output": "evalseal CLI not found"})
            continue
        out = subprocess.run(  # noqa: S603 - resolved evalseal binary; fixed arguments
            [exe, "verify", "--ledger", str(ledger), "--artifacts", "--index", str(i)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        text = out.stdout + out.stderr
        # The CLI exits non-zero only for `changed`; a missing or unverifiable bound file
        # is a warning there, but it fails verification here.
        statuses = [ln.split()[0] for ln in text.splitlines() if ln.strip()]
        bad = [st for st in statuses if st in ("changed", "missing", "unverifiable")]
        records.append({"index": i, "ok": out.returncode == 0 and not bad, "output": text[-2000:]})
    return {
        "chain": {"ok": chain_ok, "message": chain_msg},
        "signatures": {"ok": sig_ok, "message": sig_msg},
        "artifacts": records,
        "ok": chain_ok and sig_ok and all(r["ok"] for r in records),
    }
