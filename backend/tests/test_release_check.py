"""Release preconditions (scripts/release_check.py, production review A12).

The release used to accept a commit when check runs with the CI job names had succeeded (any
app or workflow can create a check run with any name) and never compared the tag with the
versions the packages declare. Now the qualifying run is found through the Actions API by
workflow file, event, branch, repository and commit, every required job of that run must
have succeeded, and the tag must equal every declared version.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from opsbridge.config import REPO_ROOT

_spec = importlib.util.spec_from_file_location(
    "release_check", REPO_ROOT / "scripts/release_check.py"
)
assert _spec is not None and _spec.loader is not None
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

REPO = "owner/opsbridge"
SHA = "a" * 40


def run(**changes: Any) -> dict[str, Any]:
    base = {
        "id": 7,
        "run_number": 12,
        "run_attempt": 1,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
    }
    return base | changes


def jobs(run_id: int = 7, **conclusions: str) -> list[dict[str, Any]]:
    return [
        {"name": name, "run_id": run_id, "conclusion": conclusions.get(f"j{i}", "success")}
        for i, name in enumerate(rc.REQUIRED_JOBS)
    ]


def test_the_ci_job_names_are_the_ones_ci_runs() -> None:
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    for name in rc.REQUIRED_JOBS:
        assert f"    name: {name}\n" in ci


def test_a_push_to_main_whose_jobs_all_succeeded_qualifies() -> None:
    assert rc.ci_problems([run()], lambda run_id: jobs(run_id), SHA, REPO) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"head_sha": "b" * 40},  # another commit
        {"event": "pull_request"},  # a PR head is not the merge commit
        {"head_branch": "feature"},
        {"path": ".github/workflows/lookalike.yml"},  # same job names, another workflow
        {"head_repository": {"full_name": "fork/opsbridge"}},
        {"repository": {"full_name": "someone/else"}},
        {"conclusion": "failure"},
        {"status": "in_progress", "conclusion": None},
    ],
    ids=["other_commit", "pr_event", "other_branch", "other_workflow", "fork", "other_repo",
         "failed", "unfinished"],
)  # fmt: skip
def test_a_run_that_is_not_ci_on_main_for_this_commit_does_not_qualify(
    changes: dict[str, Any],
) -> None:
    [problem] = rc.ci_problems([run(**changes)], lambda run_id: jobs(run_id), SHA, REPO)
    assert "no successful run" in problem and SHA in problem


def test_every_required_job_must_have_succeeded_in_that_run() -> None:
    failed = rc.ci_problems([run()], lambda run_id: jobs(run_id, j2="failure"), SHA, REPO)
    assert failed == [f"CI job {rc.REQUIRED_JOBS[2]!r} in run 7 concluded failure"]
    missing = rc.ci_problems([run()], lambda run_id: jobs(run_id)[:3], SHA, REPO)
    assert missing == [f"CI run 7 has no job {rc.REQUIRED_JOBS[3]!r}"]
    elsewhere = rc.ci_problems([run()], lambda run_id: jobs(8), SHA, REPO)  # jobs of another run
    assert len(elsewhere) == len(rc.REQUIRED_JOBS)


def test_the_newest_successful_attempt_is_the_one_read() -> None:
    runs = [run(id=5, run_number=10), run(id=9, run_number=11), run(id=11, conclusion="failure",
                                                                     run_number=12)]  # fmt: skip
    assert rc.qualifying_run(runs, SHA, REPO)["id"] == 9


def test_the_tag_must_name_the_version_every_package_declares() -> None:
    declared = {"backend/pyproject.toml": "1.2.3", "frontend/package.json": "1.2.3"}
    assert rc.version_problems("v1.2.3", declared) == []
    assert rc.version_problems("v1.2.4", declared) == [
        "backend/pyproject.toml declares version 1.2.3, the tag says 1.2.4",
        "frontend/package.json declares version 1.2.3, the tag says 1.2.4",
    ]
    for tag in ("1.2.3", "v1.2", "v01.2.3", "v1.2.3-rc1", "main"):
        assert rc.version_problems(tag, declared) == [f"tag {tag!r} is not vMAJOR.MINOR.PATCH"]


def test_every_declaration_in_the_repository_is_read(tmp_path: Path) -> None:
    current = rc.package_versions(REPO_ROOT)
    assert set(current) == {
        "backend/pyproject.toml",
        "backend/src/opsbridge/__init__.py",
        "backend/uv.lock",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/package-lock.json (root package)",
    }
    assert len(set(current.values())) == 1, current  # they agree today
    # One file left behind by a version bump is caught.
    for path in ("backend/pyproject.toml", "backend/src/opsbridge/__init__.py", "backend/uv.lock",
                 "frontend/package.json", "frontend/package-lock.json"):  # fmt: skip
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / path, tmp_path / path)
    package = json.loads((tmp_path / "frontend/package.json").read_text())
    (tmp_path / "frontend/package.json").write_text(json.dumps(package | {"version": "9.9.9"}))
    tag = "v" + current["backend/pyproject.toml"]
    assert rc.version_problems(tag, rc.package_versions(tmp_path)) == [
        f"frontend/package.json declares version 9.9.9, the tag says {tag[1:]}"
    ]
