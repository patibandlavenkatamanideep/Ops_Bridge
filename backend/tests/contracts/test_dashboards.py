"""Grafana dashboards query only what the application exports: every metric name and every
label in a `by (...)` grouping or `{…}` matcher must exist, so a typo cannot leave a panel
silently empty (the same failure as the alert rule that could never fire, docs/case-study)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from prometheus_client.metrics import MetricWrapperBase

from opsbridge.config import REPO_ROOT
from opsbridge.telemetry import metrics

GRAFANA = REPO_ROOT / "infra" / "observability" / "grafana"
# Series from Prometheus itself and from the client's Linux process collector.
EXTERNAL = {"up": {"job", "instance"}, "process_resident_memory_bytes": {"job", "instance"}}
SUFFIXES = ("_total", "_bucket", "_sum", "_count", "_created")


def _exported() -> dict[str, set[str]]:
    """Metric name → label names, from the metric objects and the custom collectors."""
    out: dict[str, set[str]] = {}
    for obj in vars(metrics).values():
        if isinstance(obj, MetricWrapperBase):
            labels = set(obj._labelnames)
            if obj._type == "histogram":
                labels.add("le")
            out[obj._name] = labels
    source = Path(metrics.__file__).read_text()
    for name, rest in re.findall(r'GaugeMetricFamily\(\s*"([a-z_]+)",([^)]*)\)', source):
        found = re.search(r"labels=\[([^\]]*)\]", rest)
        out[name] = set(re.findall(r'"([a-z_]+)"', found.group(1))) if found else set()
    for name, labels in EXTERNAL.items():
        out[name] = labels
    return out


def _base(series: str) -> str:
    for suffix in SUFFIXES:
        if series.endswith(suffix):
            return series[: -len(suffix)]
    return series


def _queries() -> list[tuple[str, str]]:
    dash = json.loads((GRAFANA / "dashboards" / "opsbridge-overview.json").read_text())
    return [
        (p["title"], t["expr"]) for p in dash["panels"] if p["type"] != "row" for t in p["targets"]
    ]


def test_panels_use_the_provisioned_datasource() -> None:
    ds = yaml.safe_load((GRAFANA / "provisioning" / "datasources" / "prometheus.yml").read_text())
    uid = ds["datasources"][0]["uid"]
    dash = json.loads((GRAFANA / "dashboards" / "opsbridge-overview.json").read_text())
    assert dash["uid"] == "opsbridge-overview" and dash["editable"] is False
    panels = [p for p in dash["panels"] if p["type"] != "row"]
    assert len(panels) >= 20
    for p in panels:
        assert p["datasource"]["uid"] == uid and all(
            t["datasource"]["uid"] == uid for t in p["targets"]
        )


def test_every_metric_and_label_in_the_dashboard_is_exported() -> None:
    exported = _exported()
    for title, expr in _queries():
        names = {
            m
            for m in re.findall(r"\b([a-z_]+)(?=\s*[{\[)]|\s*$|\s+/)", expr)
            if m.startswith(("opsbridge_", "process_")) or m == "up"
        }
        assert names, (title, expr)
        labels = set(re.findall(r"\bby \(([^)]*)\)", expr))
        labels = {lbl.strip() for group in labels for lbl in group.split(",")}
        labels |= set(re.findall(r"[{,]\s*([a-z_]+)\s*=~?", expr))
        labels |= set(re.findall(r"ignoring\(([a-z_]+)\)", expr))
        known: set[str] = set()
        for name in names:
            assert _base(name) in exported, f"{title}: unknown metric {name}"
            known |= exported[_base(name)]
        assert labels <= known | {"job", "instance"}, f"{title}: unknown labels {labels - known}"
