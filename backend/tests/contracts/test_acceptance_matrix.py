"""docs/acceptance-scenarios.md stays true: the same 24 scenarios as the brief (§19), and
every test it names exists, so the matrix cannot quietly point at nothing."""

from __future__ import annotations

import re

from opsbridge.config import REPO_ROOT

MATRIX = REPO_ROOT / "docs" / "acceptance-scenarios.md"


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _brief_scenarios() -> list[tuple[str, str]]:
    brief = (REPO_ROOT / "OPSBRIDGE_BUILD_PROMPT.md").read_text()
    section = brief.split("## 19. Required acceptance scenarios", 1)[1].split("\n## ", 1)[0]
    rows = [
        ln for ln in section.splitlines() if ln.startswith("| ") and not ln.startswith("| Scenario")
    ]
    return [(c[0], c[1]) for c in map(_cells, rows)]


def _matrix_rows() -> list[list[str]]:
    lines = MATRIX.read_text().splitlines()
    return [_cells(ln) for ln in lines if re.match(r"^\| \d+ \|", ln)]


def test_matrix_lists_exactly_the_briefs_scenarios_in_order() -> None:
    brief = _brief_scenarios()
    assert len(brief) == 24
    assert [(r[1], r[2]) for r in _matrix_rows()] == brief


def test_every_named_test_exists() -> None:
    for row in _matrix_rows():
        refs = re.findall(r"`([^`]+)`", row[3])
        assert refs, f"scenario {row[0]} names no test"
        for ref in refs:
            if "::" in ref:
                path, name = ref.split("::")
                source = (REPO_ROOT / path).read_text()
                assert re.search(rf"^def {re.escape(name)}\(", source, re.M), ref
            else:
                path, title = ref.split(" \u203a ", 1)  # the separator Playwright prints in titles
                assert f'test("{title}"' in (REPO_ROOT / path).read_text(), ref
