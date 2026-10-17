"""Frozen benchmark manifest: what a comparison is allowed to depend on, by digest.

`freeze` records the corpus splits (with case ids), document rendering, policies, prompt
templates, scoring definitions, modes, repeat schedule and acceptance policy. `verify`
re-hashes them before a benchmark run; any drift stops the run, because results produced
from changed inputs are not comparable with the frozen plan. Code is deliberately not
frozen (validation-driven fixes are allowed); the code version of each run is recorded
with its results instead.
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


def inputs(root: Path) -> dict[str, Any]:
    """The frozen inputs, recomputed from disk."""
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
            root / "evals" / "acceptance-policy.json",
        ]
    )
    return {
        "splits": corpora,
        "files": {_rel(p, root): file_digest(p) for p in files},
        "prompts": prompt_manifest(),
    }


def freeze(root: Path, benchmark_id: str, *, modes: list[str], repeats: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "benchmark_id": benchmark_id,
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "frozen_from_commit": _git_head(root),
        "modes": modes,
        "repeats": repeats,
        "scoring": SCORING,
        **inputs(root),
    }
    body["manifest_sha256"] = digest({k: v for k, v in body.items() if k != "manifest_sha256"})
    return body


def verify(manifest: dict[str, Any], root: Path) -> list[str]:
    """Problems that make a run incomparable with the frozen plan (empty = comparable)."""
    problems: list[str] = []
    if manifest.get("schema") != SCHEMA:
        problems.append(f"unknown manifest schema {manifest.get('schema')!r}")
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if digest(body) != manifest.get("manifest_sha256"):
        problems.append("the manifest file itself was edited after freezing")
    try:
        now = inputs(root)
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
    return problems


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
