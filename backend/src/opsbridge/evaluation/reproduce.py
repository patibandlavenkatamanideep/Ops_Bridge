"""Does a replay reproduce the recorded run it replays? Compared per case and repeat, by
verdict (the next action, or the error). A replay that differs is a failure, not a new result:
the recorded responses are the same, so a difference means the code or its environment
changed what OpsBridge decides."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def recorded_results(results_dir: Path, cassette: Path) -> dict[str, Any]:
    """The results file of the record run whose cassette is replayed."""
    path = results_dir / f"{cassette.parent.name}.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def differences(recorded: dict[str, Any], replayed: dict[str, dict[str, Any]]) -> list[str]:
    """Per-case verdict differences between a recorded run's modes and a replay's modes
    (summaries as produced by metrics.summarize_mode); empty means reproduced."""
    out: list[str] = []
    for mode, rec in sorted(recorded["modes"].items()):
        if mode not in replayed:
            out.append(f"{mode}: not replayed")
            continue
        a = {cid: c["verdicts"] for cid, c in rec["cases"].items()}
        b = {cid: c["verdicts"] for cid, c in replayed[mode]["cases"].items()}
        for cid in sorted(set(a) | set(b)):
            if a.get(cid) != b.get(cid):
                out.append(f"{mode} {cid}: recorded {a.get(cid)} → replay {b.get(cid)}")
    return out
