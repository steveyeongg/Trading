"""Auth + entitlements: tier limits, dev-mode resolution, /v1/me, tier caps."""

from __future__ import annotations

import pytest
from atlas_shared import for_tier, within_limit
from atlas_shared.auth import AuthError, resolve_context
from fastapi.testclient import TestClient

# --- entitlements ----------------------------------------------------------


def test_tier_matrix_progression() -> None:
    assert for_tier("free").watchlist_size == 10
    assert for_tier("pro").watchlist_size == 100
    assert for_tier("elite").watchlist_size == 500
    assert for_tier("quant").watchlist_size is None  # unlimited
    assert for_tier("enterprise").watchlist_size is None


def test_unknown_tier_degrades_to_free() -> None:
    assert for_tier("bogus").tier == "free"


def test_within_limit_unlimited() -> None:
    assert within_limit(10_000, None) is True
    assert within_limit(11, 10) is False
    assert within_limit(10, 10) is True


def test_asset_classes_widen_by_tier() -> None:
    assert "crypto" not in for_tier("free").asset_classes
    assert "crypto" in for_tier("pro").asset_classes
    assert "option" in for_tier("quant").asset_classes


def test_channels_widen_by_tier() -> None:
    assert "telegram" not in for_tier("free").channels
    assert "telegram" in for_tier("elite").channels
    assert "webhook" in for_tier("quant").channels


# --- dev-mode resolution ---------------------------------------------------


def test_dev_mode_default_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_AUTH_MODE", "dev")
    ctx = resolve_context(authorization=None)
    assert ctx.tier == "free"
    assert ctx.user_id == "dev-user"


def test_dev_mode_header_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_AUTH_MODE", "dev")
    ctx = resolve_context(authorization=None, dev_user="steve", dev_tier="elite")
    assert ctx.tier == "elite"
    assert ctx.user_id == "steve"
    assert ctx.entitlements.broker_autotrade is True


def test_jwt_mode_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_AUTH_MODE", "jwt")
    with pytest.raises(AuthError):
        resolve_context(authorization=None)


# --- /v1/me + tier caps via the API ---------------------------------------


def test_me_reflects_dev_tier() -> None:
    from signal_service.main import app

    with TestClient(app) as client:
        r = client.get("/v1/me", headers={"X-Dev-Tier": "pro", "X-Dev-User": "steve"})
        assert r.status_code == 200
        body = r.json()
        assert body["tier"] == "pro"
        assert body["entitlements"]["watchlist_size"] == 100


def test_watchlist_cap_rejects_over_limit_free_tier() -> None:
    from signal_service.main import app

    with TestClient(app) as client:
        # 11 symbols on free (cap 10) → 403, checked before any DB write.
        syms = [f"SYM{i}" for i in range(11)]
        r = client.put("/v1/watchlist", json={"symbols": syms}, headers={"X-Dev-Tier": "free"})
        assert r.status_code == 403
        assert "limit" in r.json()["detail"].lower()


def test_alert_channel_blocked_on_free_tier() -> None:
    from signal_service.main import app

    with TestClient(app) as client:
        # telegram isn't available on free → 403 before touching the store.
        r = client.post(
            "/v1/alerts",
            json={"name": "x", "metric": "composite", "op": ">=", "threshold": 80, "channels": ["telegram"]},
            headers={"X-Dev-Tier": "free"},
        )
        assert r.status_code == 403
        assert "telegram" in r.json()["detail"]
