"""Backfilling a tool cassette for a run recorded before tool cassettes existed: every entry
must match the digests in that run's own trial records, or nothing is written."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opsbridge.canonical import digest
from opsbridge.evaluation.cassette import (
    TOOL_CASSETTE,
    ReplayFailure,
    backfill_tool_cassette,
    load_tool_entries,
)

ARGS = {"query": "delivered"}
HITS = [{"document_id": "d1", "version_id": "v1", "sha256": "sha256:" + "a" * 64, "text": "x"}]


def recorded_run(tmp_path: Path, hits: Any = HITS) -> tuple[Path, Path]:
    run, store = tmp_path / "run", tmp_path / "artifacts" / "tenant"
    run.mkdir()
    store.mkdir(parents=True)
    (run / "model-cassette.json").write_text(json.dumps({"benchmark_id": "b", "split": "s"}))
    call = {
        "seq": 1,
        "tool": "search_case_evidence",
        "version": "1",
        "input_sha256": digest(ARGS),
        "response_sha256": digest(HITS),
        "outcome": "ok",
    }
    trial = {"case_id": "c1", "mode": "mock", "repeat": 0, "tool_calls": [call]}
    (run / "trials-mock.jsonl").write_text(json.dumps(trial) + "\n")
    (run / "trials-rules_only.jsonl").write_text(json.dumps({**trial, "tool_calls": []}) + "\n")
    (store / "req").write_text(json.dumps({"tool": "search_case_evidence", "args": ARGS}))
    (store / "resp").write_text(json.dumps(hits))
    (store / "doc.pdf").write_bytes(b"%PDF-1.4 not json")
    (store / "usage").write_text(json.dumps({"latency": 0.25}))  # floats: no canonical digest
    return run, tmp_path / "artifacts"


def test_entries_come_from_kept_artifacts_that_match_the_recorded_digests(
    tmp_path: Path,
) -> None:
    run, artifacts = recorded_run(tmp_path)
    out = backfill_tool_cassette(run, artifacts)
    assert out == run / TOOL_CASSETTE
    body = json.loads(out.read_text())
    assert (body["benchmark_id"], body["split"], body["response_mode"]) == ("b", "s", "live")
    [entry] = body["entries"]
    assert entry["args"] == ARGS and entry["response"] == HITS
    assert load_tool_entries(out)[("c1", "mock", 0, 1)] == entry
    with pytest.raises(FileExistsError):
        backfill_tool_cassette(run, artifacts)  # never overwritten


def test_a_response_that_differs_from_the_recorded_digest_writes_nothing(tmp_path: Path) -> None:
    run, artifacts = recorded_run(tmp_path, hits=[{**HITS[0], "text": "edited"}])
    with pytest.raises(ReplayFailure, match="no kept response artifact matches case=c1"):
        backfill_tool_cassette(run, artifacts)
    assert not (run / TOOL_CASSETTE).exists()
