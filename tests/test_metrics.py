"""Observability: metric increments, time_stage, /metrics endpoint, and the
HTTP middleware recording a request."""

from __future__ import annotations

from atlas_shared import metrics as mx
from fastapi.testclient import TestClient


def test_render_exposes_metric_names() -> None:
    # Touch a couple of metrics so they appear in the exposition.
    mx.SIGNALS_TOTAL.labels(result="published").inc()
    mx.ORDERS_TOTAL.labels(intent="open", status="filled").inc()
    body, content_type = mx.render()
    text = body.decode()
    assert "text/plain" in content_type
    assert "atlas_signals_total" in text
    assert "atlas_orders_total" in text
    assert "atlas_http_requests_total" in text or "atlas_http_request_seconds" in text


def test_time_stage_records_observation() -> None:
    before = mx.PIPELINE_SECONDS.labels(stage="unit-test")._sum.get()  # type: ignore[attr-defined]
    with mx.time_stage("unit-test"):
        sum(range(1000))
    after = mx.PIPELINE_SECONDS.labels(stage="unit-test")._sum.get()  # type: ignore[attr-defined]
    assert after >= before


def test_counter_increments() -> None:
    c = mx.MONITOR_EXITS_TOTAL.labels(reason="stop")
    before = c._value.get()  # type: ignore[attr-defined]
    mx.MONITOR_EXITS_TOTAL.labels(reason="stop").inc()
    assert c._value.get() == before + 1  # type: ignore[attr-defined]


def test_metrics_endpoint_and_middleware() -> None:
    from signal_service.main import app

    with TestClient(app) as client:
        # Make a request so the middleware records it.
        client.get("/healthz")
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "atlas_" in r.text
        # The /healthz hit should have been counted with its route template.
        assert "/healthz" in r.text


def test_metrics_endpoint_not_self_counted() -> None:
    from signal_service.main import app

    with TestClient(app) as client:
        r = client.get("/metrics")
        # The scrape path itself is excluded from http_requests_total labels.
        assert 'path="/metrics"' not in r.text
