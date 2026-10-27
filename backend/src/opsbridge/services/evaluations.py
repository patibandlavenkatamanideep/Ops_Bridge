"""Read-only access to benchmark results for the evaluation view.

Benchmark corpora are synthetic and belong to no tenant. A tenant sees each run's aggregate
metrics and comparability data, but per-case detail only for its own purchasing pack.
Files are read from the configured evals directory by a validated run id, never by a path
from the request.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from opsbridge.auth.permissions import Forbidden, Principal
from opsbridge.evaluation.manifest import scheme_of

RUN_ID = re.compile(r"^(dev|validation|heldout|security)-(record|replay)-\d{8}T\d{6}Z$")
BENCHMARK = re.compile(r"^[a-z0-9-]{1,64}$")


class EvalNotFound(Exception):
    pass


def require_eval_access(p: Principal) -> None:
    if not (p.has("eval.read") or p.has("investigation.run")):
        raise Forbidden("eval.read")


def _results(evals_dir: Path) -> list[Path]:
    return sorted((evals_dir / "results").glob("*/*.json"), reverse=True)


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


# Shown with every scoring-1 run: what its recorded "flip rate" measured (review A03).
SCORING_1_INSTABILITY = (
    "Scoring 1 measured disagreement in whether the next action was correct, not in the "
    "action itself: repeats giving two different wrong actions count as stable. The numbers "
    "are kept as recorded; scoring 2 reports action, correctness and outcome disagreement "
    "separately."
)


def _plan(evals_dir: Path, benchmark_id: str) -> dict[str, Any]:
    path = evals_dir / "manifests" / f"{benchmark_id}.json"
    return _load(path) if BENCHMARK.match(benchmark_id) and path.is_file() else {}


def _scheme(evals_dir: Path, d: dict[str, Any]) -> str | None:
    """The scheme a run was graded under: its result says so, or its frozen plan does."""
    return d.get("scoring") or scheme_of(_plan(evals_dir, d["benchmark_id"]).get("scoring"))


def _comparability(evals_dir: Path, d: dict[str, Any]) -> dict[str, Any]:
    """Whether a run was graded under the benchmark's current frozen plan and this code's
    scoring; if not, its numbers must not be compared with current runs."""
    reasons = []
    plan = _plan(evals_dir, d["benchmark_id"])
    if not plan:
        reasons.append("the benchmark's frozen plan is missing")
    elif d.get("manifest_sha256") != plan.get("manifest_sha256"):
        reasons.append("graded under a different frozen plan than the current one")
    if plan and scheme_of(plan.get("scoring")) is None:
        reasons.append(
            "this code's scoring definitions differ from the frozen plan (no scheme it "
            "implements matches)"
        )
    return {
        "comparable": not reasons,
        "reasons": reasons,
        "note": None
        if not reasons
        else "Not comparable with current runs: a change of grading or plan, "
        "not evidence of a model or rules regression.",
    }


def _mode_headline(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "next_action": m["next_action"],
        "case": m["case"],
        "safe_misses": m["safe_misses"],
        "unsafe_misses": m["unsafe_misses"],
        "trial_errors": m["trial_errors"],
    }


def list_runs(evals_dir: Path, p: Principal) -> list[dict[str, Any]]:
    require_eval_access(p)
    out = []
    for path in _results(evals_dir):
        d = _load(path)
        if d.get("schema") != "opsbridge-eval-results/1" or not RUN_ID.match(d.get("run_id", "")):
            continue
        out.append(
            {
                "benchmark_id": d["benchmark_id"],
                "run_id": d["run_id"],
                "split": d["split"],
                "kind": d["kind"],
                "repeats": d["repeats"],
                "code_commit": (d.get("code_version") or {}).get("commit"),
                "modes": {k: _mode_headline(v) for k, v in d["modes"].items()},
                "acceptance": d.get("acceptance", []),
                "receipts_verified": bool((d.get("verification") or {}).get("ok")),
                "comparability": _comparability(evals_dir, d),
                "scoring_scheme": _scheme(evals_dir, d),
            }
        )
    return out


def _find(evals_dir: Path, benchmark: str, run_id: str) -> dict[str, Any]:
    if not BENCHMARK.match(benchmark) or not RUN_ID.match(run_id):
        raise EvalNotFound(run_id)
    path = evals_dir / "results" / benchmark / f"{run_id}.json"
    if not path.is_file():
        raise EvalNotFound(run_id)
    return _load(path)


def run_detail(evals_dir: Path, p: Principal, benchmark: str, run_id: str) -> dict[str, Any]:
    require_eval_access(p)
    d = _find(evals_dir, benchmark, run_id)
    scheme = _scheme(evals_dir, d)
    case_keys = ("family", "template", "expected", "majority", "verdicts", "correct", "safe_miss")
    # Scoring 1 recorded a flip rate per case; scoring 2 records disagreement per axis.
    extra_keys = ("disagreement", "complete", "problems", "whole_verdict", "flip_rate")
    modes = {}
    for mode, m in d["modes"].items():
        cases = [
            {
                "case_id": cid,
                **{k: c[k] for k in case_keys},
                **{k: c[k] for k in extra_keys if k in c},
            }
            for cid, c in sorted(m["cases"].items())
            if c["pack"] == p.tenant_pack
        ]
        modes[mode] = {
            **{
                k: m[k]
                for k in (
                    "n_cases",
                    "n_trials",
                    "trial_errors",
                    "next_action",
                    "outcome",
                    "types",
                    "case",
                    "safe_misses",
                    "unsafe_misses",
                    "by_type",
                    "by_expected_outcome",
                    "instability",
                    "agent",
                    "latency_ms",
                )
            },
            **{k: m[k] for k in ("complete", "incomplete_cases") if k in m},
            "instability_note": SCORING_1_INSTABILITY if scheme == "opsbridge-scoring/1" else None,
            "pack_cases": cases,
        }
    return {
        "benchmark_id": d["benchmark_id"],
        "run_id": d["run_id"],
        "split": d["split"],
        "kind": d["kind"],
        "repeats": d["repeats"],
        "manifest_sha256": d["manifest_sha256"],
        "response_origin": d["response_origin"],
        "network": d["network"],
        "code_version": d["code_version"],
        "pack": p.tenant_pack,
        "modes": modes,
        "paired_rules_vs_mock": d.get("paired_rules_vs_mock"),
        "acceptance": d.get("acceptance", []),
        "verification": {
            "ok": bool((d.get("verification") or {}).get("ok")),
            "chain": (d.get("verification") or {}).get("chain", {}).get("message"),
            "signatures": (d.get("verification") or {}).get("signatures", {}).get("message"),
        },
        "receipts": d.get("receipts", {}),
        "comparability": _comparability(evals_dir, d),
        "scoring_scheme": scheme,
        "not_evaluated": [
            "Real-model quality (no live or recorded real-model dataset; mock responses only)",
            "Operator time and business outcomes (no pilot participants)",
        ],
    }


def receipt_records(
    evals_dir: Path, p: Principal, benchmark: str, run_id: str
) -> list[dict[str, Any]]:
    """The sealed EvalSeal records of this run (from the ledger), as stored."""
    require_eval_access(p)
    d = _find(evals_dir, benchmark, run_id)
    hashes = {r["record_hash"] for r in d.get("receipts", {}).values()}
    ledger = evals_dir / "ledger" / "opsbridge-bench.jsonl"
    records = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("hash") in hashes:
                records.append(rec)
    return records
