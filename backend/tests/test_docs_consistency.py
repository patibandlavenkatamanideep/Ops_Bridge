"""The documents name commands, settings and commits that exist (review G: status drift).

A runbook that names a `make` target or a CLI command that is not there fails when someone
follows it; a setting the code no longer reads is configured for nothing; and two documents
naming different commits for the same release claim cannot both be current. Found by this
test when it was written: `make eval-verify` (named in docs/deployment.md, missing from the
Makefile, now added). It checks names only: whether what a document says is true stays a
matter of reading it against the code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from opsbridge.cli import build_parser
from opsbridge.config import REPO_ROOT, Settings

DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "CLAUDE.md",
    *sorted((REPO_ROOT / "docs").glob("*.md")),
]
CODE = [
    *(REPO_ROOT / "backend" / "src").rglob("*.py"),
    *(REPO_ROOT / "backend" / "tests").rglob("*.py"),
    *(REPO_ROOT / ".github" / "workflows").glob("*.yml"),
    REPO_ROOT / "Makefile",
    *(REPO_ROOT / "scripts").glob("*.py"),
]
# Read from the environment by code, not settings: test switches and the trace file.
NOT_SETTINGS = {
    "OPSBRIDGE_REQUIRE_INTEGRATION",
    "OPSBRIDGE_REQUIRE_SANDBOX",
    "OPSBRIDGE_REQUIRE_OCR",
    "OPSBRIDGE_RUN_LIVE",
    "OPSBRIDGE_TRACE_FILE",
}
PREFIXES = ("OPSBRIDGE_CONNECTION_SECRET_",)  # one per registered credential reference


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _found(pattern: str, flags: int = 0) -> set[tuple[str, str]]:
    return {(d.name, m) for d in DOCS for m in re.findall(pattern, _text(d), flags)}


def test_every_make_target_a_document_names_exists() -> None:
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", _text(REPO_ROOT / "Makefile"), re.M))
    named = _found(r"`make ([a-z][a-z0-9-]*)") | _found(r"^\s{4,}make ([a-z][a-z0-9-]*)", re.M)
    assert {(doc, t) for doc, t in named if t not in targets} == set()


def test_every_cli_command_a_document_names_exists() -> None:
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    commands = set(sub.choices)  # type: ignore[arg-type]
    named = _found(r"(?:python -m opsbridge\.cli|`opsbridge) ([a-z][a-z0-9-]*)")
    assert {(doc, c) for doc, c in named if c not in commands} == set()


def test_every_setting_a_document_names_is_read_by_the_code() -> None:
    settings = {"OPSBRIDGE_" + name.upper() for name in Settings.model_fields}
    named = _found(r"OPSBRIDGE_[A-Z0-9_]*[A-Z0-9](?!\.md)\b")
    unknown = {
        (doc, v)
        for doc, v in named
        if v not in settings | NOT_SETTINGS and not v.startswith(PREFIXES)
        and not v.startswith("OPSBRIDGE_BUILD_PROMPT")  # the brief's file name
    }  # fmt: skip
    assert unknown == set()
    code = "\n".join(_text(p) for p in CODE if p.resolve() != Path(__file__).resolve())
    assert {v for v in NOT_SETTINGS if v not in code} == set()  # the list cannot go stale


@pytest.mark.parametrize(
    ("document", "claim"),
    [
        ("README.md", r"Hosted CI \| \*\*Passing\*\* on `main` at `([0-9a-f]{7})`"),
        ("README.md", r"`v0\.1\.0-alpha\.1` qualifies on `main`'s head, `([0-9a-f]{7})`"),
        ("docs/production-readiness.md", r"\| A12 \|.*?on `main`'s head, `([0-9a-f]{7})`"),
    ],
)
def test_the_release_claims_name_one_commit(document: str, claim: str) -> None:
    """The commit CI passed on `main`, the one the first tag qualifies on, and the one the
    readiness record names are the same; a merge updates them together."""
    readme = _text(REPO_ROOT / "README.md")
    [reference] = re.findall(r"Hosted CI \| \*\*Passing\*\* on `main` at `([0-9a-f]{7})`", readme)
    assert re.findall(claim, _text(REPO_ROOT / document)) == [reference]
