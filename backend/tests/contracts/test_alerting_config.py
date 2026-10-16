"""Alert rules, routing and runbooks agree: every rule pages or tickets someone through a
receiver that also reports resolution, and links to a runbook section that exists."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from opsbridge.config import REPO_ROOT

OBS = REPO_ROOT / "infra" / "observability"


def _rules() -> list[dict[str, Any]]:
    doc = yaml.safe_load((OBS / "prometheus" / "alerts.yml").read_text())
    return [r for g in doc["groups"] for r in g["rules"] if "alert" in r]


def _anchors(path: Path) -> set[str]:
    heads = re.findall(r"^#+ (.+)$", path.read_text(), flags=re.M)
    return {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in heads}


def _receiver_for(route: dict[str, Any], labels: dict[str, str]) -> str:
    for child in route.get("routes", []):
        if all(_matches(m, labels) for m in child["matchers"]):
            return _receiver_for(child, labels) if child.get("routes") else child["receiver"]
    return str(route["receiver"])


def _matches(matcher: str, labels: dict[str, str]) -> bool:
    name, value = re.fullmatch(r'(\w+)="([^"]*)"', matcher).groups()  # type: ignore[union-attr]
    return labels.get(name) == value


def test_every_rule_has_severity_summary_and_existing_runbook() -> None:
    rules = _rules()
    assert len(rules) >= 15
    anchors = _anchors(REPO_ROOT / "docs" / "operations.md")
    for r in rules:
        assert r["labels"]["severity"] in {"page", "ticket"}, r["alert"]
        assert r["annotations"]["summary"], r["alert"]
        doc, _, anchor = r["annotations"]["runbook"].partition("#")
        assert doc == "docs/operations.md" and anchor in anchors, (r["alert"], anchor)


def test_each_severity_reaches_its_own_receiver_which_reports_resolution() -> None:
    config = yaml.safe_load((OBS / "alertmanager" / "alertmanager.local.yml").read_text())
    receivers = {r["name"]: r for r in config["receivers"]}
    for r in _rules():
        severity = r["labels"]["severity"]
        assert _receiver_for(config["route"], {"alertname": r["alert"], **r["labels"]}) == severity
    assert _receiver_for(config["route"], {"alertname": "NoSeverity"}) == "ticket"  # never dropped
    for name, receiver in receivers.items():
        hooks = receiver.get("webhook_configs", [])
        assert hooks and all(h.get("send_resolved") for h in hooks), name


def test_routes_file_covers_both_receivers() -> None:
    lines = [
        ln.split()
        for ln in (OBS / "alertmanager" / "routes.test").read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert {ln[0] for ln in lines} == {"page", "ticket"}
