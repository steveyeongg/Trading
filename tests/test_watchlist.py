"""Watchlist: symbol cleaning + the read-fallback route (no DB)."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient
from signal_service.watchlist import MAX_SYMBOLS, _clean


def _postgres_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=0.25):
            return True
    except OSError:
        return False


def test_clean_uppercases_dedupes_trims() -> None:
    assert _clean([" aapl ", "AAPL", "msft", "", "  ", "nvda"]) == ["AAPL", "MSFT", "NVDA"]


def test_clean_preserves_order() -> None:
    assert _clean(["TSLA", "AAPL", "TSLA", "MSFT"]) == ["TSLA", "AAPL", "MSFT"]


def test_clean_caps_length() -> None:
    syms = [f"SYM{i}" for i in range(100)]
    assert len(_clean(syms)) == MAX_SYMBOLS


@pytest.mark.skipif(_postgres_reachable(), reason="local Postgres is up — test requires no-DB env")
def test_read_watchlist_falls_back_without_db() -> None:
    # No Postgres → the route must still 200 with the fallback list.
    from signal_service.main import app

    with TestClient(app) as client:
        r = client.get("/v1/watchlist")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["symbols"], list)
        assert "AAPL" in body["symbols"]
        assert body.get("fallback") is True


@pytest.mark.skipif(_postgres_reachable(), reason="local Postgres is up — test requires no-DB env")
def test_update_watchlist_503_without_db() -> None:
    from signal_service.main import app

    with TestClient(app) as client:
        r = client.put("/v1/watchlist", json={"symbols": ["AAPL", "MSFT"]})
        # Write needs Postgres; without it we surface a clean 503, not a 500.
        assert r.status_code == 503


def test_read_watchlist_route_returns_200_in_any_state() -> None:
    """The read route must never error — it always returns 200, either with
    real symbols or with the hardcoded fallback. We don't assert which here:
    pytest's lifespan + event-loop reuse can make the broad-except path flip
    to fallback even when the DB is reachable. The shape is what matters."""
    from signal_service.main import app

    with TestClient(app) as client:
        r = client.get("/v1/watchlist")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["symbols"], list)
        assert all(isinstance(s, str) for s in body["symbols"])
