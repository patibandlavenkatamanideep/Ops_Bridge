"""Offline evaluation guards: network egress, frozen inputs (no services needed)."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

import httpx
import pytest

from opsbridge.config import REPO_ROOT
from opsbridge.evaluation import manifest
from opsbridge.evaluation.egress import EgressBlocked, loopback_only


def test_egress_guard_blocks_lookups_and_non_loopback_connections() -> None:
    with loopback_only():
        with pytest.raises(EgressBlocked):
            httpx.get("https://api.anthropic.com/v1/messages", timeout=1)
        with pytest.raises(EgressBlocked):
            socket.create_connection(("203.0.113.10", 443), timeout=1)  # TEST-NET-3, never routed
        with pytest.raises(EgressBlocked):
            socket.getaddrinfo("github.com", 443)
        # Loopback stays reachable (simulator, local services).
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
    # The guard is removed afterwards.
    assert socket.getaddrinfo("localhost", 80)


def _copy_inputs(dst: Path) -> None:
    for rel in ["fixtures/scenarios", "packs", "evals/acceptance-policy.json"]:
        src = REPO_ROOT / rel
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst / rel)
        else:
            shutil.copy2(src, dst / rel)
    sim = dst / "backend/src/opsbridge/simulator"
    sim.mkdir(parents=True)
    for name in ("scenarios.py", "minipdf.py"):
        shutil.copy2(REPO_ROOT / "backend/src/opsbridge/simulator" / name, sim / name)


def test_frozen_manifest_detects_any_input_drift(tmp_path: Path) -> None:
    _copy_inputs(tmp_path)
    frozen = manifest.freeze(tmp_path, "test-bench", modes=["rules_only"], repeats=3)
    assert manifest.verify(frozen, tmp_path) == []
    held = tmp_path / "fixtures/scenarios/heldout.yaml"
    held.write_text(
        held.read_text().replace(
            "next_action: escalate:assign_case", "next_action: no_discrepancy:record_review_note", 1
        )
    )
    policy = tmp_path / "packs/furniture/policies/furniture-ap-v2.yaml"
    policy.write_text(policy.read_text().replace('absolute: "0.50"', 'absolute: "0.75"'))
    problems = manifest.verify(frozen, tmp_path)
    assert any("split heldout changed" in p for p in problems)
    assert any("furniture-ap-v2.yaml changed" in p for p in problems)
    edited = {**frozen, "repeats": 1}
    assert any("edited after freezing" in p for p in manifest.verify(edited, tmp_path))


def test_committed_benchmark_manifest_matches_the_repository() -> None:
    frozen = manifest.load(REPO_ROOT / "evals/manifests/opsbridge-bench-v1.json")
    assert manifest.verify(frozen, REPO_ROOT) == []
