#!/usr/bin/env python3
"""Pair a recorded run's rules-only and agent-assisted (mock) verdicts case by case.

Equal totals do not say that two modes missed the same cases. This reads one recorded run's
trial logs and model cassette (evals/runs/<benchmark>/<split>-record-<time>/), writes nothing,
and prints:
- the paired counts: both modes right, both wrong, and the cases where they differ;
- for each case the modes disagree on, and each case both miss: the expected next action,
  every repeat's action in each mode, and what the investigation had when it decided (the
  finding codes and citations it recorded, and what the mock model was recorded answering).

Only the recording is read, so what it shows is what was recorded then. Standard library
only.

  python3 scripts/bench_pairing.py
  python3 scripts/bench_pairing.py --run evals/runs/opsbridge-bench-v1/<split>-record-<time>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HELDOUT = ROOT / "evals/runs/opsbridge-bench-v1/heldout-record-20261015T022744Z"
MODES = ("rules_only", "mock")


def trials(run: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    by_case: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    for mode in MODES:
        for line in (run / f"trials-{mode}.jsonl").read_text(encoding="utf-8").splitlines():
            t = json.loads(line)
            by_case[t["case_id"]].setdefault(mode, []).append(t)
    return by_case


def answers(run: Path) -> dict[tuple[str, int], list[str]]:
    """What the mock model was recorded answering, per case and repeat."""
    cassette = json.loads((run / "model-cassette.json").read_text(encoding="utf-8"))
    out: dict[tuple[str, int], list[str]] = defaultdict(list)
    for e in sorted(cassette["entries"], key=lambda e: (e["case_id"], e["repeat"], e["seq"])):
        out[(e["case_id"], e["repeat"])].append(f"{e['template_id']}: {e['response']['text']}")
    return out


def majority(ts: list[dict[str, Any]]) -> str:
    return str(Counter(t["next_action"] for t in ts).most_common(1)[0][0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", type=Path, default=HELDOUT)
    run = parser.parse_args(argv).run
    by_case, said = trials(run), answers(run)
    both = neither = 0
    shown: list[tuple[str, str]] = []
    for case in sorted(by_case):
        modes = by_case[case]
        if set(modes) != set(MODES):
            print(f"{case}: recorded in {sorted(modes)} only; it cannot be paired")
            return 1
        expected = modes["rules_only"][0]["expected"]["next_action"]
        right = {m: majority(modes[m]) == expected for m in MODES}
        if all(right.values()):
            both += 1
        elif not any(right.values()):
            neither += 1
            shown.append((case, "both miss"))
        else:
            shown.append((case, "the modes disagree"))
    print(f"run {run.relative_to(ROOT) if run.is_relative_to(ROOT) else run}")
    print(
        f"{len(by_case)} cases: both right {both}, both wrong {neither}, "
        f"discordant {len(shown) - neither}"
    )
    for case, kind in shown:
        modes = by_case[case]
        first = modes["rules_only"][0]
        print(f"\n{case} ({first['family']}, {first['template']}): {kind}")
        print(f"  expected: {first['expected']['next_action']} ({first['expected']['note']})")
        for mode in MODES:
            ts = modes[mode]
            print(f"  {mode}: " + ", ".join(t["next_action"] for t in ts))
            codes = sorted({c for t in ts for c in t["finding_codes"]})
            cites = sorted({(t["citations"], t["citations_valid"]) for t in ts})
            print(f"    finding codes {codes}; citations (made, valid) {cites}")
        for text in said.get((case, 0), []):
            print(f"    mock, repeat 0, {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
