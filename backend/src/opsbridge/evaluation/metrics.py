"""Benchmark metrics computed from trial records (definitions frozen in the manifest).

The unit is the case: repeats are aggregated by majority verdict per case before any
case-level proportion, and repeats are never counted as extra cases. A trial that errors
counts as incorrect. Intervals are Wilson 95% over cases; the rules-vs-agent comparison is
paired by case.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Any

from evalseal import exact_mcnemar, flip_rate, wilson_ci

TYPES = ("missing_receipt", "quantity_mismatch", "price_mismatch")
ESCALATE = "escalate:assign_case"


def _majority(values: list[Any]) -> Any:
    """Most common value; ties go to the earliest value (deterministic, documented)."""
    counts = Counter(values)
    top = max(counts.values())
    return next(v for v in values if counts[v] == top)


def _ci(k: int, n: int) -> list[float] | None:
    return [round(x, 4) for x in wilson_ci(k, n)] if n else None


def _rate(k: int, n: int) -> dict[str, Any]:
    return {"correct": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": _ci(k, n)}


def per_case(trials: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trials:
        by_case[t["case_id"]].append(t)
    out: dict[str, dict[str, Any]] = {}
    for case_id, ts in by_case.items():
        ts = sorted(ts, key=lambda t: t["repeat"])
        verdicts = [t["next_action"] if not t["error"] else f"error:{t['error'][:60]}" for t in ts]
        majority_action = _majority(verdicts)
        rep = next(
            t
            for t in ts
            if (t["next_action"] if not t["error"] else f"error:{t['error'][:60]}")
            == majority_action
        )
        binary = [1 if t["correct"]["next_action"] and not t["error"] else 0 for t in ts]
        rate, _ = flip_rate(binary)
        expected = ts[0]["expected"]
        ok_action = rep["correct"]["next_action"] and not rep["error"]
        out[case_id] = {
            "family": ts[0]["family"],
            "template": ts[0]["template"],
            "pack": ts[0]["pack"],
            "expected": expected,
            "verdicts": verdicts,
            "majority": {
                "next_action": None if rep["error"] else rep["next_action"],
                "outcome": rep["outcome"],
                "types": rep["types"],
                "error": rep["error"],
            },
            "correct": {
                "next_action": ok_action,
                "outcome": rep["correct"]["outcome"] and not rep["error"],
                "types": rep["correct"]["types"] and not rep["error"],
                "case": rep["correct"]["case"] and not rep["error"],
            },
            "safe_miss": (not ok_action) and rep["next_action"] == ESCALATE and not rep["error"],
            "flip_rate": round(rate, 4),
            "finding_codes": rep["finding_codes"],
        }
    return out


def instability(cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """How often repeats of one case disagree, and what the repeat count cannot show."""
    n = len(cases)
    flipping = sum(1 for c in cases.values() if c["flip_rate"] > 0)
    repeats = sorted({len(c["verdicts"]) for c in cases.values()})
    fewest = repeats[0] if repeats else 0
    if fewest < 2:
        limit = "fewer than 2 repeats for some case: instability cannot be measured"
    else:
        limit = (
            f"{fewest} repeats per case: a per-case flip rate below 1/{fewest} cannot be "
            f"observed, and no flips in {fewest} repeats do not show that a case is stable; "
            f"the interval for flipping cases is over {n} cases"
        )
    return {
        "definition": "non-majority verdicts / repeats (EvalSeal flip_rate)",
        "cases_with_any_flip": flipping,
        "cases_with_any_flip_rate": _rate(flipping, n),
        "mean_flip_rate": round(sum(c["flip_rate"] for c in cases.values()) / n, 4) if n else None,
        "repeats_per_case": repeats,
        "sampling_limit": limit,
    }


def summarize_mode(trials: list[dict[str, Any]]) -> dict[str, Any]:
    cases = per_case(trials)
    n = len(cases)
    k = {
        key: sum(1 for c in cases.values() if c["correct"][key])
        for key in ("next_action", "outcome", "types", "case")
    }
    by_type: dict[str, Any] = {}
    for typ in TYPES:
        tp = sum(
            1
            for c in cases.values()
            if typ in c["majority"]["types"] and typ in c["expected"]["types"]
        )
        fp = sum(
            1
            for c in cases.values()
            if typ in c["majority"]["types"] and typ not in c["expected"]["types"]
        )
        fn = sum(
            1
            for c in cases.values()
            if typ not in c["majority"]["types"] and typ in c["expected"]["types"]
        )
        by_type[typ] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        }
    misses = {cid: c for cid, c in cases.items() if not c["correct"]["next_action"]}
    origins = Counter(t["explanation_origin"] for t in trials if not t["error"])
    latencies = sorted(t["latency_ms"] for t in trials)
    validations = [t["proposal_validation"] for t in trials if t.get("proposal_validation")]
    return {
        "n_cases": n,
        "n_trials": len(trials),
        "trial_errors": sum(1 for t in trials if t["error"]),
        "next_action": _rate(k["next_action"], n),
        "outcome": _rate(k["outcome"], n),
        "types": _rate(k["types"], n),
        "case": _rate(k["case"], n),
        "safe_misses": sum(1 for c in misses.values() if c["safe_miss"]),
        "unsafe_misses": sum(1 for c in misses.values() if not c["safe_miss"]),
        "by_type": by_type,
        "by_expected_outcome": {
            o: _rate(
                sum(
                    1
                    for c in cases.values()
                    if c["expected"]["outcome"] == o and c["correct"]["next_action"]
                ),
                sum(1 for c in cases.values() if c["expected"]["outcome"] == o),
            )
            for o in ("matched", "discrepancies_found", "needs_information", "manual_review")
        },
        "instability": instability(cases),
        "agent": {
            "explanation_origins": dict(origins),
            "model_calls": sum(t["model_calls"] for t in trials),
            "model_calls_rejected": sum(t["model_calls_rejected"] for t in trials),
            "tool_calls": sum(len(t["tool_calls"]) for t in trials),
            "citations": sum(t["citations"] for t in trials),
            "citations_valid": sum(t["citations_valid"] for t in trials),
            "proposals_validated": len(validations),
            "proposals_rejected": sum(1 for v in validations if not v.get("accepted", True)),
            "input_tokens": sum(t["model_input_tokens"] for t in trials),
            "output_tokens": sum(t["model_output_tokens"] for t in trials),
            "cost_usd": "0 (mock or replay; no paid calls)",
        },
        "latency_ms": {
            "median": median(latencies) if latencies else None,
            "p95": latencies[int(0.95 * (len(latencies) - 1))] if latencies else None,
            "note": "wall time per investigation run on the local machine",
        },
        "misses": {
            cid: {
                "family": c["family"],
                "template": c["template"],
                "expected": c["expected"]["next_action"],
                "got": c["majority"]["next_action"],
                "error": c["majority"]["error"],
                "safe_miss": c["safe_miss"],
                "finding_codes": c["finding_codes"],
            }
            for cid, c in sorted(misses.items())
        },
        "cases": cases,
    }


def paired(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Paired by case on next-action correctness. `regressed` = a right, b wrong."""
    common = sorted(set(a["cases"]) & set(b["cases"]))
    both = sum(
        1
        for c in common
        if a["cases"][c]["correct"]["next_action"] and b["cases"][c]["correct"]["next_action"]
    )
    neither = sum(
        1
        for c in common
        if not a["cases"][c]["correct"]["next_action"]
        and not b["cases"][c]["correct"]["next_action"]
    )
    regressed = [
        c
        for c in common
        if a["cases"][c]["correct"]["next_action"] and not b["cases"][c]["correct"]["next_action"]
    ]
    improved = [
        c
        for c in common
        if not a["cases"][c]["correct"]["next_action"] and b["cases"][c]["correct"]["next_action"]
    ]
    return {
        "n_cases": len(common),
        "both_correct": both,
        "neither_correct": neither,
        "regressed": regressed,
        "improved": improved,
        "exact_mcnemar_p": exact_mcnemar(len(regressed), len(improved))
        if regressed or improved
        else None,
        "note": "no discordant cases: the comparison cannot distinguish the modes on this split"
        if not (regressed or improved)
        else "",
    }


def acceptance(summary: dict[str, Any], policy: dict[str, Any], split: str) -> list[dict[str, Any]]:
    """Evaluate the frozen held-out next-action gate for each mode; other gates are reported
    from their own evidence (test runs) and are not decided here."""
    gate = next(g for g in policy["gates"] if g["id"] == "heldout_next_action")
    out = []
    for mode in gate["applies_to"]:
        s = summary["modes"].get(mode)
        if split != "heldout" or s is None:
            out.append(
                {
                    "gate": gate["id"],
                    "mode": mode,
                    "status": "not_evaluated",
                    "reason": f"split {split}",
                }
            )
            continue
        rate, ci = s["next_action"]["rate"], s["next_action"]["wilson95"]
        status = "pass" if rate is not None and rate >= gate["threshold"] else "fail"
        detail = ""
        if status == "pass" and ci and ci[0] < gate["threshold"]:
            detail = "pass (interval includes values below the target)"
        out.append(
            {
                "gate": gate["id"],
                "mode": mode,
                "status": status,
                "detail": detail,
                "rate": rate,
                "wilson95": ci,
                "threshold": gate["threshold"],
                "denominator": s["n_cases"],
            }
        )
    return out
