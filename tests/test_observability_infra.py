"""Guard the observability infra: YAML/JSON parse, and the Grafana dashboard +
Prometheus alerts only reference metric families that actually exist in
atlas_shared.metrics. Catches drift when a metric is renamed/removed."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OBS = ROOT / "infra" / "observability"

# Metric *family* names defined in atlas_shared.metrics (without _total/_bucket
# /_count/_sum suffixes that Prometheus appends).
KNOWN = {
    "atlas_signals_total",
    "atlas_alerts_fired_total",
    "atlas_orders_total",
    "atlas_monitor_exits_total",
    "atlas_pipeline_seconds",
    "atlas_http_requests_total",
    "atlas_http_request_seconds",
    "atlas_ws_connections",
}

_METRIC_RE = re.compile(r"atlas_[a-z_]+")


def _normalise(name: str) -> str:
    for suffix in ("_bucket", "_count", "_sum"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def test_yaml_files_parse() -> None:
    for rel in [
        "docker-compose.yml",
        "prometheus/prometheus.yml",
        "prometheus/alerts.yml",
        "grafana/provisioning/datasources/prometheus.yml",
        "grafana/provisioning/dashboards/dashboards.yml",
    ]:
        with (OBS / rel).open() as f:
            assert yaml.safe_load(f) is not None, rel


def test_dashboard_is_valid_json() -> None:
    data = json.loads((OBS / "grafana" / "dashboards" / "atlas.json").read_text())
    assert data["title"]
    assert data["panels"]


def test_dashboard_metrics_are_known() -> None:
    text = (OBS / "grafana" / "dashboards" / "atlas.json").read_text()
    referenced = {_normalise(m) for m in _METRIC_RE.findall(text)}
    unknown = referenced - KNOWN
    assert not unknown, f"dashboard references unknown metrics: {unknown}"


def test_alert_metrics_are_known() -> None:
    text = (OBS / "prometheus" / "alerts.yml").read_text()
    referenced = {_normalise(m) for m in _METRIC_RE.findall(text)}
    unknown = referenced - KNOWN
    assert not unknown, f"alerts reference unknown metrics: {unknown}"


def test_known_set_matches_module() -> None:
    """The KNOWN set here must equal the families actually defined in code."""
    import atlas_shared.metrics  # noqa: F401  (registers metrics)
    from prometheus_client import REGISTRY

    defined = {
        name
        for name in REGISTRY._names_to_collectors  # type: ignore[attr-defined]
        if name.startswith("atlas_")
    }
    defined_families = {_normalise(n) for n in defined}
    # Every dashboard/alert metric we assert on must be a real family.
    assert defined_families >= KNOWN, f"KNOWN has stale names: {KNOWN - defined_families}"


@pytest.mark.parametrize("port_line", ["3001:3000", "9090:9090"])
def test_compose_exposes_expected_ports(port_line: str) -> None:
    compose = (OBS / "docker-compose.yml").read_text()
    assert port_line in compose
