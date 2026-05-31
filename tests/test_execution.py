"""Execution: paper broker fills, broker registry, tier gating (no DB needed
for the broker/gate paths)."""

from __future__ import annotations

import pytest
from execution_service.brokers import build_broker
from execution_service.brokers.base import OrderRequest
from execution_service.brokers.paper import PaperBroker
from fastapi.testclient import TestClient

# --- paper broker ----------------------------------------------------------


async def test_paper_fills_at_limit_price() -> None:
    b = PaperBroker()
    assert b.available
    fill = await b.submit(OrderRequest(symbol="AAPL", side="buy", quantity=10, limit_price=200.0))
    assert fill.ok and fill.status == "filled"
    assert fill.fill_price == 200.0
    assert fill.broker_order_id and fill.broker_order_id.startswith("paper-")


async def test_paper_fills_at_reference_when_no_limit() -> None:
    b = PaperBroker()
    fill = await b.submit(OrderRequest(symbol="AAPL", side="buy", quantity=5, reference_price=214.3))
    assert fill.ok and fill.fill_price == 214.3


async def test_paper_rejects_without_price() -> None:
    b = PaperBroker()
    fill = await b.submit(OrderRequest(symbol="AAPL", side="buy", quantity=5))
    assert not fill.ok and fill.status == "rejected"


async def test_paper_rejects_nonpositive_qty() -> None:
    b = PaperBroker()
    fill = await b.submit(OrderRequest(symbol="AAPL", side="buy", quantity=0, limit_price=10))
    assert not fill.ok


def test_registry_falls_back_to_paper_without_alpaca(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert build_broker().name == "paper"


# --- tier gating via the API ----------------------------------------------


def test_execute_blocked_for_free_tier() -> None:
    from signal_service.main import app

    with TestClient(app) as client:
        r = client.post(
            "/v1/execute",
            json={"symbol": "AAPL", "intent": "open", "quantity": 10, "limit_price": 200},
            headers={"X-Dev-Tier": "free"},
        )
        assert r.status_code == 403
        assert "broker execution requires" in r.json()["detail"].lower()


def test_execute_allowed_past_gate_for_elite() -> None:
    from signal_service.main import app

    with TestClient(app) as client:
        # Elite clears the entitlement gate; without a DB the order record write
        # fails → 503. That proves we got *past* the tier check.
        r = client.post(
            "/v1/execute",
            json={"symbol": "AAPL", "intent": "open", "quantity": 10, "limit_price": 200},
            headers={"X-Dev-Tier": "elite"},
        )
        assert r.status_code in (200, 503)
        assert r.status_code != 403
