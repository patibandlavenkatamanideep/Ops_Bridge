#!/usr/bin/env python3
"""Release preconditions (production review A12), checked by .github/workflows/release.yml
before an image is built. Standard library only: it runs on the hosted runner's Python.

  versions --tag v1.2.3       the tag is vMAJOR.MINOR.PATCH and both packages declare it
  ci --sha SHA --repo O/R     a successful run of this repository's CI workflow, started by a
                              push to main, qualified exactly this commit, and every required
                              job of that run succeeded

A check run's name is not evidence: any app or workflow with access to checks can create one
with any name. The run is found through the Actions API by workflow file, event, branch,
repository and head commit, and its jobs are read from that run. Exit status 1 with the
reasons on stderr when a precondition fails.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = ".github/workflows/ci.yml"
REQUIRED_JOBS = (
    "lint, types, unit, canary, receipts, build",
    "integration (PostgreSQL, Keycloak, MCP) and alert rules",
    "browser tests against the dev stack",
    "runtime image (build, smoke test, SBOM, vulnerability scan)",
)
_TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def package_versions(root: Path = ROOT) -> dict[str, str]:
    """Every place the release version is declared, by file (a lock that disagrees with its
    manifest would release one version under another's name)."""

    def text(path: str) -> str:
        return (root / path).read_text(encoding="utf-8")

    init = re.search(r'^__version__ = "([^"]*)"$', text("backend/src/opsbridge/__init__.py"), re.M)
    locked = [
        p["version"]
        for p in tomllib.loads(text("backend/uv.lock")).get("package", [])
        if p.get("name") == "opsbridge"
    ]
    npm_lock = json.loads(text("frontend/package-lock.json"))
    return {
        "backend/pyproject.toml": str(
            tomllib.loads(text("backend/pyproject.toml"))["project"]["version"]
        ),
        "backend/src/opsbridge/__init__.py": init.group(1) if init else "(none)",
        "backend/uv.lock": locked[0] if len(locked) == 1 else "(none)",
        "frontend/package.json": str(json.loads(text("frontend/package.json"))["version"]),
        "frontend/package-lock.json": str(npm_lock.get("version")),
        "frontend/package-lock.json (root package)": str(
            (npm_lock.get("packages") or {}).get("", {}).get("version")
        ),
    }


def version_problems(tag: str, versions: dict[str, str]) -> list[str]:
    if not _TAG.match(tag):
        return [f"tag {tag!r} is not vMAJOR.MINOR.PATCH"]
    return [
        f"{path} declares version {declared}, the tag says {tag[1:]}"
        for path, declared in versions.items()
        if declared != tag[1:]
    ]


def qualifying_run(runs: list[dict[str, Any]], sha: str, repo: str) -> dict[str, Any] | None:
    """The newest successful CI run of `repo`'s own workflow file, started by a push to main,
    whose head is exactly `sha`."""
    matching = [
        r
        for r in runs
        if r.get("head_sha") == sha
        and r.get("path") == CI_WORKFLOW
        and r.get("event") == "push"
        and r.get("head_branch") == "main"
        and (r.get("repository") or {}).get("full_name") == repo
        and (r.get("head_repository") or {}).get("full_name") == repo
        and r.get("status") == "completed"
        and r.get("conclusion") == "success"
    ]
    return max(
        matching, key=lambda r: (r.get("run_number", 0), r.get("run_attempt", 0)), default=None
    )


def job_problems(jobs: list[dict[str, Any]], run_id: int) -> list[str]:
    """Required jobs missing from the run's latest attempt, or not successful there."""
    by_name = {j.get("name"): j for j in jobs if j.get("run_id") == run_id}
    problems = []
    for name in REQUIRED_JOBS:
        job = by_name.get(name)
        if job is None:
            problems.append(f"CI run {run_id} has no job {name!r}")
        elif job.get("conclusion") != "success":
            problems.append(f"CI job {name!r} in run {run_id} concluded {job.get('conclusion')}")
    return problems


def ci_problems(runs: list[dict[str, Any]], jobs_of: Any, sha: str, repo: str) -> list[str]:
    run = qualifying_run(runs, sha, repo)
    if run is None:
        return [
            f"no successful run of {CI_WORKFLOW} started by a push to main of {repo} "
            f"qualified {sha}"
        ]
    return job_problems(jobs_of(int(run["id"])), int(run["id"]))


def _gh(path: str) -> Any:
    # The GitHub CLI preinstalled on hosted runners; the path is built from fixed parts.
    out = subprocess.run(  # noqa: S603
        ["gh", "api", path],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    versions = sub.add_parser("versions")
    versions.add_argument("--tag", required=True)
    ci = sub.add_parser("ci")
    ci.add_argument("--sha", required=True)
    ci.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    if args.command == "versions":
        problems = version_problems(args.tag, package_versions())
        ok = f"tag {args.tag} matches the package versions"
    else:
        workflow = Path(CI_WORKFLOW).name
        runs = _gh(
            f"repos/{args.repo}/actions/workflows/{workflow}/runs"
            f"?head_sha={args.sha}&event=push&branch=main&status=success&per_page=100"
        )["workflow_runs"]
        problems = ci_problems(
            runs,
            lambda run_id: _gh(
                f"repos/{args.repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
            )["jobs"],
            args.sha,
            args.repo,
        )
        ok = f"CI qualified {args.sha} (push to main, every required job succeeded)"
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    if not problems:
        print(ok)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
