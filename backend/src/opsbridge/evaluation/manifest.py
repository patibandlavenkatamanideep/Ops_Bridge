"""Frozen benchmark manifest: what a comparison is allowed to depend on, by digest.

`freeze` records the corpus splits (with case ids), document rendering, policies, prompt
templates, scoring definitions, modes, repeat schedule, the acceptance policy its runs are
judged by, the investigation workflow version its trials run and, optionally, what the plan
is for. `verify` re-hashes them before a benchmark
run; any drift stops the run, because results produced from changed inputs are not
comparable with the frozen plan. Code is deliberately not frozen (validation-driven fixes are
allowed); the code version of each run is recorded with its results instead. Behaviour that
changes what a recorded run asked the model is versioned as a workflow instead, so a plan's
recordings replay with the workflow that made them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opsbridge.agents.prompts import manifest as prompt_manifest
from opsbridge.canonical import digest
from opsbridge.simulator.scenarios import load_corpus
from opsbridge.workflow.investigation import (
    LEGACY_WORKFLOW_VERSION,
    WORKFLOW_VERSION,
    WORKFLOW_VERSIONS,
)

SCHEMA = "opsbridge-eval-manifest/1"
SPLITS = ("dev", "validation", "heldout", "security")

SCORING = {
    "unit": "case; repeats are aggregated by majority verdict per case before case-level metrics",
    "primary": "next_action_correct: decision:action_type equals the hand-written label",
    "secondary": [
        "outcome_correct: outcome equals the label",
        "types_correct: the set of discrepancy types equals the label",
        "case_correct: outcome, types and next action all correct",
        "safe_miss: incorrect next action whose actual route is escalate:assign_case "
        "(a person reviews) — reported separately, still counted as incorrect",
        "per-type precision and recall with counts, over majority verdicts",
        "agent mode: explanation origin, rejected model outputs, citation validity",
    ],
    "instability": "non-majority verdicts / total repeats per case (EvalSeal flip_rate); "
    "not the adjacent-transition rate",
    "uncertainty": "Wilson 95% interval over cases for proportions; paired rules-vs-agent "
    "comparison by case with discordant counts and an exact McNemar test",
}

# Scoring 1 (above) is frozen in opsbridge-bench-v1 and stays exactly as written: its
# "instability" was in fact disagreement in next-action CORRECTNESS (two different wrong
# actions counted as stable). Scoring 2 reports each kind of disagreement separately.
# A frozen plan is graded under the scheme it names; new plans are frozen under the latest.
SCORING_2 = {
    "scheme": "opsbridge-scoring/2",
    "unit": "case; repeats are aggregated per case before case-level metrics and are never "
    "counted as extra cases",
    "primary": "next_action_correct: the majority next action (decision:action_type) equals "
    "the hand-written label",
    "majority": "taken per axis (next action, outcome, set of discrepancy types) over all "
    "repeats; an errored repeat is a verdict of its own ('error') and is never correct; ties "
    "go to the value of the earliest repeat and are reported",
    "secondary": [
        "outcome_correct and types_correct from their own axis majorities",
        "case_correct: the most common whole verdict (action, outcome, types) equals the label; "
        "a tie between whole verdicts is never counted correct",
        "safe_miss: incorrect majority next action that is escalate:assign_case, still incorrect",
        "per-type precision and recall with counts, over the types majority",
        "agent mode: explanation origin, rejected model outputs, citation validity",
    ],
    "disagreement": {
        "action": "1 - (count of the most common next action, errors as 'error') / repeats",
        "correctness": "1 - (count of the more common of right/wrong next action) / repeats",
        "outcome_types": "1 - (count of the most common (outcome, types) pair) / repeats",
        "errors": "repeats that errored / repeats",
    },
    "completeness": "a case must have exactly repeats 0..R-1 once each under one label; "
    "otherwise it is incomplete, listed, and the acceptance gate is not decided",
    "uncertainty": "Wilson 95% interval over cases for proportions; paired rules-vs-agent "
    "comparison by case with discordant counts and an exact McNemar test",
}
SCHEMES: dict[str, dict[str, Any]] = {
    "opsbridge-scoring/1": SCORING,
    "opsbridge-scoring/2": SCORING_2,
}
CURRENT_SCHEME = "opsbridge-scoring/2"


def scheme_of(scoring: Any) -> str | None:
    """The scheme a frozen plan's scoring definitions are, or None if this code has none."""
    return next((name for name, body in SCHEMES.items() if body == scoring), None)


# Plans frozen before plans named their policy (opsbridge-bench-v1) are judged by this one.
DEFAULT_ACCEPTANCE_POLICY = "evals/acceptance-policy.json"


def acceptance_policy_of(manifest: dict[str, Any]) -> str:
    """The acceptance policy a plan's runs are judged by (a repository-relative path)."""
    return str(manifest.get("acceptance_policy", DEFAULT_ACCEPTANCE_POLICY))


def load_acceptance_policy(manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    """The plan's acceptance policy; refused if it was declared for another benchmark, whose
    gates (a held-out gate above all) would not mean the same thing for this plan."""
    policy: dict[str, Any] = json.loads(
        (root / acceptance_policy_of(manifest)).read_text(encoding="utf-8")
    )
    if policy.get("benchmark_id") != manifest.get("benchmark_id"):
        raise ValueError(
            f"{acceptance_policy_of(manifest)} was declared for {policy.get('benchmark_id')}, "
            f"not {manifest.get('benchmark_id')}"
        )
    return policy


def frozen_plans(root: Path) -> list[str]:
    """Benchmark ids with a frozen plan in the repository."""
    return sorted(p.stem for p in (root / "evals" / "manifests").glob("*.json"))


def workflow_version_of(manifest: dict[str, Any]) -> str:
    """The investigation workflow a plan's trials run. Plans frozen before plans named one
    (opsbridge-bench-v1) were recorded with investigation/1.1.0."""
    return str(manifest.get("workflow_version", LEGACY_WORKFLOW_VERSION))


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def _git_head(root: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv: resolved git binary, read-only query
            [git, "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def code_version(root: Path) -> dict[str, Any]:
    """Commit of the code that ran, and whether code or inputs had uncommitted changes."""
    git = shutil.which("git")
    dirty = None
    if git is not None:
        try:
            out = subprocess.run(  # noqa: S603 - fixed argv: resolved git binary, read-only query
                # Code and benchmark inputs only: appending to the evaluation ledger is not a
                # code change.
                [
                    git,
                    "-C",
                    str(root),
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                    "--",
                    "backend",
                    "frontend",
                    "packs",
                    "fixtures",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            dirty = bool(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            dirty = None
    return {"commit": _git_head(root), "uncommitted_changes": dirty}


def inputs(root: Path, acceptance_policy: str = DEFAULT_ACCEPTANCE_POLICY) -> dict[str, Any]:
    """The frozen inputs, recomputed from disk, with the plan's own acceptance policy."""
    corpora: dict[str, Any] = {}
    for split in SPLITS:
        path = root / "fixtures" / "scenarios" / f"{split}.yaml"
        corpus = load_corpus(path)
        wrong = [s.id for s in corpus.scenarios if s.split != split]
        if wrong:
            raise ValueError(f"{path} contains scenarios of another split: {wrong[:5]}")
        corpora[split] = {
            "path": _rel(path, root),
            "sha256": file_digest(path),
            "corpus_id": corpus.corpus_id,
            "n_cases": len(corpus.scenarios),
            "case_ids": [s.id for s in corpus.scenarios],
            "families": sorted({s.family for s in corpus.scenarios}),
            "templates": sorted({s.template for s in corpus.scenarios}),
        }
    files = sorted(
        [
            # Policies are business inputs; pack mappings are system configuration under test
            # (recorded with each run's code version, like the rest of the code).
            *root.glob("packs/*/policies/*.yaml"),
            root / "fixtures" / "scenarios" / "author_dev.py",
            root / "fixtures" / "scenarios" / "author_eval.py",
            # Rendering is part of the inputs: it turns scenarios into the external documents.
            root / "backend" / "src" / "opsbridge" / "simulator" / "scenarios.py",
            root / "backend" / "src" / "opsbridge" / "simulator" / "minipdf.py",
            root / acceptance_policy,
        ]
    )
    return {
        "splits": corpora,
        "files": {_rel(p, root): file_digest(p) for p in files},
        "prompts": prompt_manifest(),
    }


def freeze(
    root: Path,
    benchmark_id: str,
    *,
    modes: list[str],
    repeats: int,
    scheme: str = CURRENT_SCHEME,
    acceptance_policy: str = DEFAULT_ACCEPTANCE_POLICY,
    purpose: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "benchmark_id": benchmark_id,
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "frozen_from_commit": _git_head(root),
        "modes": modes,
        "repeats": repeats,
        "scoring": SCHEMES[scheme],
        "workflow_version": WORKFLOW_VERSION,
        "acceptance_policy": acceptance_policy,
        **({"purpose": purpose} if purpose else {}),
        **inputs(root, acceptance_policy),
    }
    body["manifest_sha256"] = digest({k: v for k, v in body.items() if k != "manifest_sha256"})
    return body


SCORING_CHANGED = (
    "the frozen plan's scoring definitions are not a scheme this code implements: results "
    "would be graded differently, so they are not comparable with this benchmark's earlier "
    "runs (a grading change, not evidence of a model or rules change); freeze a new "
    "benchmark id"
)


def verify(manifest: dict[str, Any], root: Path) -> list[str]:
    """Problems that make a run incomparable with the frozen plan (empty = comparable)."""
    problems: list[str] = []
    if manifest.get("schema") != SCHEMA:
        problems.append(f"unknown manifest schema {manifest.get('schema')!r}")
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if digest(body) != manifest.get("manifest_sha256"):
        problems.append("the manifest file itself was edited after freezing")
    try:
        now = inputs(root, acceptance_policy_of(manifest))
    except (OSError, ValueError) as exc:
        return [*problems, f"frozen inputs unreadable: {exc}"]
    for split, frozen in manifest.get("splits", {}).items():
        cur = now["splits"].get(split)
        if cur is None:
            problems.append(f"split {split} is missing")
        elif cur["sha256"] != frozen["sha256"] or cur["case_ids"] != frozen["case_ids"]:
            problems.append(f"split {split} changed since freezing ({frozen['path']})")
    for path, sha in manifest.get("files", {}).items():
        if now["files"].get(path) != sha:
            problems.append(f"{path} changed since freezing")
    for path in set(now["files"]) - set(manifest.get("files", {})):
        problems.append(f"{path} was added after freezing")
    if now["prompts"] != manifest.get("prompts"):
        problems.append("prompt templates changed since freezing")
    if scheme_of(manifest.get("scoring")) is None:
        problems.append(SCORING_CHANGED)
    if workflow_version_of(manifest) not in WORKFLOW_VERSIONS:
        problems.append(
            f"the plan's trials run {workflow_version_of(manifest)}, which this code does not "
            "implement; its recordings cannot be replayed with another workflow"
        )
    return problems


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
