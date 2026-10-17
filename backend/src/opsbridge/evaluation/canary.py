"""Canary regression suite: fixed cases, offline, compared with a pinned baseline.

Runs the rules-only path (bytes → parser → pack mapping → matcher → router, no database,
no model) over the dev, validation and security corpora under the loopback-only egress
guard, and compares each case's outcome, discrepancy types, next action and finding codes
with `evals/baselines/canary-rules.json`. Any difference fails, whether it is a regression or
an improvement; an intended change updates the baseline in a reviewed commit
(`--update-baseline`). Held-out is excluded so it stays a benchmark, not a tuning set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opsbridge.domain.matching import RULES_VERSION
from opsbridge.simulator.scenarios import load_corpus

from .egress import loopback_only
from .rules_baseline import PolicyBook, evaluate

SPLITS = ("dev", "validation", "security")


def run(root: Path) -> dict[str, Any]:
    book = PolicyBook(root / "packs")
    cases: dict[str, Any] = {}
    with loopback_only():
        for split in SPLITS:
            for s in load_corpus(root / "fixtures" / "scenarios" / f"{split}.yaml").scenarios:
                v = evaluate(s, book)
                cases[s.id] = {
                    "outcome": v.outcome,
                    "types": sorted(v.types),
                    "next_action": v.next_action,
                    "finding_codes": sorted(v.finding_codes),
                    "matches_label": v.case_correct,
                }
    return {
        "schema": "opsbridge-canary/1",
        "rules_version": RULES_VERSION,
        "splits": list(SPLITS),
        "cases": cases,
    }


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    diffs = []
    if baseline.get("rules_version") != current["rules_version"]:
        diffs.append(f"rules version {baseline.get('rules_version')} → {current['rules_version']}")
    old, new = baseline.get("cases", {}), current["cases"]
    for cid in sorted(set(old) | set(new)):
        if cid not in new:
            diffs.append(f"{cid}: missing from this run")
        elif cid not in old:
            diffs.append(f"{cid}: not in the baseline")
        elif old[cid] != new[cid]:
            changed = {
                k: (old[cid].get(k), new[cid][k])
                for k in new[cid]
                if old[cid].get(k) != new[cid][k]
            }
            diffs.append(f"{cid}: {changed}")
    return diffs


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
