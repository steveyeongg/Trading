"""Per-user scoping: the user_id from auth threads into the stores, and two
users get isolated views. Stubs the store layer so no DB is needed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import signal_service.watchlist as wl

    # Stub the store so GET echoes which user_id the route resolved.
    async def _get_symbols(user_id: str) -> list[str]:
        return [f"OWNED_BY_{user_id}"]

    monkeypatch.setattr(wl, "get_symbols", _get_symbols)
    from signal_service.main import app

    return TestClient(app)


def test_watchlist_is_per_user(client: TestClient) -> None:
    a = client.get("/v1/watchlist", headers={"X-Dev-User": "alice"}).json()
    b = client.get("/v1/watchlist", headers={"X-Dev-User": "bob"}).json()
    assert a["symbols"] == ["OWNED_BY_alice"]
    assert b["symbols"] == ["OWNED_BY_bob"]
    assert a["symbols"] != b["symbols"]


def test_default_dashboard_user(client: TestClient) -> None:
    # The dashboard sends no X-Dev-User in some calls → dev default applies.
    # Here we pass the dashboard's actual header to confirm pass-through.
    r = client.get("/v1/watchlist", headers={"X-Dev-User": "dashboard"}).json()
    assert r["symbols"] == ["OWNED_BY_dashboard"]


def test_store_signatures_require_user_id() -> None:
    """Guard against regressing to a global (user-less) store."""
    import inspect

    from alert_service import create_rule, delete_rule, list_rules
    from portfolio_service import build_summary, list_positions

    assert "user_id" in inspect.signature(list_rules).parameters
    assert "user_id" in inspect.signature(create_rule).parameters
    assert "user_id" in inspect.signature(delete_rule).parameters
    assert "user_id" in inspect.signature(build_summary).parameters
    assert "user_id" in inspect.signature(list_positions).parameters


def test_broadcaster_uses_all_rules_not_scoped() -> None:
    """The broadcaster must evaluate every user's rules, not one user's."""
    import inspect

    import signal_service.stream as stream

    src = inspect.getsource(stream._push_signals)
    assert "list_all_rules" in src
