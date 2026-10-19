"""Portability (OB-03): customer-specific behaviour lives in packs and policies, not in code.

Scans every string literal (docstrings excluded) in the core packages for tenant slugs, pack
ids, demo user names, synthetic document numbers and supplier codes. Any match means the
core branches on a specific organization. Seeding, the simulator and evaluation are excluded:
they handle the synthetic demo data by design.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "opsbridge"
CORE = [
    "domain",
    "evidence",
    "workflow",
    "agents",
    "services",
    "api",
    "mcp_services",
    "connectors",
    "persistence",
    "auth",
    "privacy",
    "telemetry",
    "audit",
    "operations",
]
SPECIFIC = re.compile(
    r"\bdemo-(?:furniture|it)\b|\bfurniture\b|\bit_hardware\b"
    r"|\b(?:furn|it)-(?:requester|investigator|approver2?|admin|auditor)\b"
    r"|\b(?:INV|PO|GRN?|MSG)-\d|\b(?:SUP|VEN)-[A-Z]{2,}|\bAcme\b|\bLumen\b|\bNorthgate\b"
)


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and body
        ):
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def _hits(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstrings(tree)
    return [
        f"{path.relative_to(SRC) if path.is_relative_to(SRC) else path.name}:{node.lineno}: {node.value[:60]!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docs
        and SPECIFIC.search(node.value)
    ]


@pytest.mark.parametrize("package", CORE)
def test_core_package_has_no_organization_specific_literals(package: str) -> None:
    files = sorted((SRC / package).rglob("*.py"))
    assert files, package
    assert [h for f in files for h in _hits(f)] == []


def test_the_scanner_detects_a_specific_branch(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        'def route(t):\n    return "x" if t.slug == "demo-it" else "y"\n', encoding="utf-8"
    )
    assert _hits(probe) == ["probe.py:2: 'demo-it'"]
