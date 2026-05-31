"""Tier entitlements — BLUEPRINT §17.1.

Pure data + check helpers, no I/O. `None` for a limit means *unlimited*.
The API layer enforces these; the frontend reads them to disable controls at
the cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TIERS = ("free", "pro", "elite", "quant", "enterprise")


@dataclass(frozen=True)
class Entitlements:
    tier: str
    watchlist_size: int | None          # None = unlimited
    max_alerts: int | None
    ai_explanations_per_day: int | None
    asset_classes: tuple[str, ...]
    backtest_concurrent: int
    backtest_years: int | None
    broker_autotrade: bool
    api_access: bool
    api_rate_per_min: int
    channels: tuple[str, ...] = field(default_factory=tuple)


# Derived from the §17.1 tier matrix. Channel/asset progression matches the
# blueprint; exact alert counts are chosen to be sensible.
_MATRIX: dict[str, Entitlements] = {
    "free": Entitlements(
        tier="free", watchlist_size=10, max_alerts=3, ai_explanations_per_day=5,
        asset_classes=("equity", "etf"), backtest_concurrent=1, backtest_years=1,
        broker_autotrade=False, api_access=False, api_rate_per_min=0,
        channels=("log", "email"),
    ),
    "pro": Entitlements(
        tier="pro", watchlist_size=100, max_alerts=25, ai_explanations_per_day=50,
        asset_classes=("equity", "etf", "crypto"), backtest_concurrent=3, backtest_years=5,
        broker_autotrade=False, api_access=True, api_rate_per_min=60,
        channels=("log", "email", "push"),
    ),
    "elite": Entitlements(
        tier="elite", watchlist_size=500, max_alerts=100, ai_explanations_per_day=500,
        asset_classes=("equity", "etf", "crypto", "fx", "future"),
        backtest_concurrent=10, backtest_years=15,
        broker_autotrade=True, api_access=True, api_rate_per_min=1000,
        channels=("log", "email", "push", "sms", "telegram"),
    ),
    "quant": Entitlements(
        tier="quant", watchlist_size=None, max_alerts=None, ai_explanations_per_day=5000,
        asset_classes=("equity", "etf", "crypto", "fx", "future", "option"),
        backtest_concurrent=30, backtest_years=25,
        broker_autotrade=True, api_access=True, api_rate_per_min=10000,
        channels=("log", "email", "push", "sms", "telegram", "webhook"),
    ),
    "enterprise": Entitlements(
        tier="enterprise", watchlist_size=None, max_alerts=None, ai_explanations_per_day=None,
        asset_classes=("equity", "etf", "crypto", "fx", "future", "option"),
        backtest_concurrent=100, backtest_years=None,
        broker_autotrade=True, api_access=True, api_rate_per_min=100000,
        channels=("log", "email", "push", "sms", "telegram", "webhook"),
    ),
}


def for_tier(tier: str) -> Entitlements:
    """Resolve entitlements; unknown tiers degrade to free."""
    return _MATRIX.get(tier, _MATRIX["free"])


def within_limit(value: int, limit: int | None) -> bool:
    """True if `value` is allowed under `limit` (None = unlimited)."""
    return limit is None or value <= limit


def allows_asset_class(tier: str, asset_class: str) -> bool:
    return asset_class in for_tier(tier).asset_classes
