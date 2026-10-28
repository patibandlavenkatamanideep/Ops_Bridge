"""Does a replay reproduce the recorded run it replays? A replay that differs is a failure,
not a new result: the recorded responses are the same, so a difference means the code or its
environment changed what OpsBridge did.

Two comparisons. `differences` (replay comparison 1) compares the verdict per case and repeat:
the next action, or the error. `trial_differences` (`opsbridge-replay/2`, production review
A13) compares each trial's behaviour: outcome, discrepancy types, reason codes, explanation
origin, citations, model calls, proposal validation, tool calls, and digests of the findings
(with their values and evidence) and of the citations. Ids and timings a run generates are not
behaviour and are left out; a field the recording does not have (runs recorded before it
existed) is reported as not compared, never assumed equal."""

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


COMPARISON = "opsbridge-replay/2"
BEHAVIOUR = (
    "next_action",
    "error",
    "outcome",
    "types",
    "finding_codes",
    "explanation_origin",
    "citations",
    "citations_valid",
    "model_calls",
    "model_calls_rejected",
    "run_status",
    "response_mode",
    "proposal_validation",
    "tool_calls",
    "findings_sha256",
    "citations_sha256",
)


def _behaviour(t: dict[str, Any], name: str) -> Any:
    """A trial field as behaviour: generated ids and replay provenance removed."""
    value = t.get(name)
    if name == "proposal_validation" and isinstance(value, dict):
        return {k: v for k, v in value.items() if k != "model_call_id"}
    if name == "explanation_origin" and isinstance(value, str):
        # "model:mock" when recorded, "model:replay" when served from the recording: the
        # behaviour is model versus template; the provider label is provenance.
        return value.split(":", 1)[0]
    if name == "tool_calls" and isinstance(value, list):
        # `origin` says whether the response was live or served from the recording: a
        # replay differs there by design, not in behaviour.
        return [{k: v for k, v in c.items() if k != "origin"} for c in value]
    return value


def trial_differences(
    recorded: dict[str, list[dict[str, Any]]], replayed: dict[str, list[dict[str, Any]]]
) -> tuple[list[str], list[str]]:
    """(differences, fields the recording does not have) between recorded and replayed
    trials, per mode, case and repeat. Empty differences means the behaviour reproduced."""
    out: list[str] = []
    not_recorded: set[str] = set()
    for mode in sorted(set(recorded) | set(replayed)):
        a = {(t["case_id"], t["repeat"]): t for t in recorded.get(mode, [])}
        b = {(t["case_id"], t["repeat"]): t for t in replayed.get(mode, [])}
        for key in sorted(set(a) | set(b)):
            label = f"{mode} {key[0]}#{key[1]}"
            if key not in a:
                out.append(f"{label}: not in the recording")
                continue
            if key not in b:
                out.append(f"{label}: not replayed")
                continue
            changed = []
            for name in BEHAVIOUR:
                if name not in a[key]:
                    not_recorded.add(name)
                    continue
                if _behaviour(a[key], name) != _behaviour(b[key], name):
                    changed.append(
                        f"{name} recorded {_behaviour(a[key], name)!r} → replay "
                        f"{_behaviour(b[key], name)!r}"
                    )
            if changed:
                out.append(f"{label}: " + "; ".join(changed)[:600])
    return out, sorted(not_recorded)
