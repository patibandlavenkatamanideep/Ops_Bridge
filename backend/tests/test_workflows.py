"""The workflows hold together before they run (production review A12).

The release workflow has never run: its first tag is its first test. These read the workflow
files the way Actions will and catch, before that, what would otherwise fail a release
halfway: a step output read but never written, an action not pinned to a commit, or a check
of CI's image job that the release does not repeat on the digest it publishes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from opsbridge.config import REPO_ROOT

WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
STEP_OUTPUT = re.compile(r"steps\.([\w-]+)\.outputs\.([\w-]+)")
WRITES_OUTPUT = re.compile(r"""echo\s+"?([\w-]+)=.*>>\s*"?\$\{?GITHUB_OUTPUT""")


def load(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def steps(path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    return [(name, job.get("steps", [])) for name, job in load(path)["jobs"].items()]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit(path: Path) -> None:
    for _job, job_steps in steps(path):
        for step in job_steps:
            uses = step.get("uses")
            if uses and not uses.startswith("./"):
                assert PINNED.match(uses), f"{path.name}: {uses} is not pinned to a commit"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_step_output_read_is_written_earlier_in_its_job(path: Path) -> None:
    for job, job_steps in steps(path):
        written: dict[str, set[str] | None] = {}  # None: an action's outputs (not listed here)
        for step in job_steps:
            for step_id, output in STEP_OUTPUT.findall(yaml.safe_dump(step)):
                assert step_id in written, f"{path.name} {job}: no earlier step {step_id!r}"
                names = written[step_id]
                assert names is None or output in names, (
                    f"{path.name} {job}: step {step_id!r} never writes {output!r}"
                )
            if "id" in step:
                run = step.get("run")
                written[step["id"]] = set(WRITES_OUTPUT.findall(run)) if run else None


def _image_commands(job: dict[str, Any], image: str) -> set[tuple[str, ...]]:
    """What each `docker run … IMAGE COMMAND` in the job's scripts runs: the program and its
    leading arguments, up to the first path, variable, quote or shell operator."""
    commands: set[tuple[str, ...]] = set()
    for step in job["steps"]:
        script = re.sub(r"\\\n\s*", " ", step.get("run", ""))
        for line in script.splitlines():
            found = re.search(rf"docker run\b.*?{re.escape(image)}(.*)$", line)
            if not found:
                continue
            words: list[str] = []
            for word in found.group(1).split():
                if word in (">", "||", "&&", ";", "|") or re.search(r"[/$\"']", word):
                    break
                words.append(word)
            commands.add(tuple(words))
    return commands


def test_the_release_repeats_every_check_of_ci_image_job_on_its_candidate() -> None:
    ci = load(REPO_ROOT / ".github/workflows/ci.yml")["jobs"]["image"]
    release = load(REPO_ROOT / ".github/workflows/release.yml")["jobs"]["release"]
    checked = _image_commands(ci, "opsbridge/app:ci")
    repeated = _image_commands(release, '"${CANDIDATE}"')
    assert ("python", "-m", "opsbridge.cli", "parser-sandbox-check") in checked
    assert checked - repeated == set(), "the release does not repeat these checks of CI's"
    # The same scanner gate on both.
    gates = [
        next(s["with"] for s in job["steps"] if "trivy-action" in s.get("uses", ""))
        for job in (ci, release)
    ]
    assert gates[0]["severity"] == gates[1]["severity"] == "CRITICAL,HIGH"
    assert gates[0]["ignore-unfixed"] == gates[1]["ignore-unfixed"]
