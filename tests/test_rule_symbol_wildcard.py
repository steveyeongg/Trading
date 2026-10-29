"""Regression guard for the silent-alert-filter bug.

The dashboard's alert form defaulted the "symbol" input to the literal
string `"ANY"`, and `matches()` treated that as a strict equality filter —
`signal["symbol"] == "ANY"` is False for every real ticker, so every rule
created via the UI silently filtered out every candidate. Zero alerts fired
for weeks despite composites clearly clearing the threshold.

The first fix made `"any"` and `"all"` wildcards, which broke real tickers:
a rule for Allstate (`ALL`) fired on every stock. The fix that stands:
`normalize_rule_symbol` trims and upper-cases, and only None, blank and
`"*"` mean "any ticker". The store normalises on write, and migration 0011
turns the old `"ANY"` default rows into NULL.
"""

from __future__ import annotations

import pytest
from alert_service.rules import AlertRule, matches, normalize_rule_symbol
from atlas_shared.migrate import MIGRATIONS_DIR


def _rule(symbol: str | None) -> AlertRule:
    return AlertRule(
        id="r", name="signal", metric="composite", op=">=", threshold=20.0,
        symbol=symbol, channels=("log",),
    )


def _signal(symbol: str = "QBTS", composite: float = 31.0) -> dict:
    return {"symbol": symbol, "composite_score": composite, "direction": "long"}


@pytest.mark.parametrize("symbol", [None, "", "  ", "*", " * "])
def test_wildcards_match_any_ticker(symbol: str | None) -> None:
    assert normalize_rule_symbol(symbol) is None
    assert matches(_rule(symbol), _signal()) is True


@pytest.mark.parametrize("ticker", ["ALL", "ANY", "all", "any"])
def test_real_tickers_all_and_any_stay_literal(ticker: str) -> None:
    """Allstate is `ALL` and Sphere 3D is `ANY`: a rule for either must not
    fire on every other stock."""
    assert normalize_rule_symbol(ticker) == ticker.upper()
    assert matches(_rule(ticker), _signal(symbol="QBTS")) is False
    assert matches(_rule(ticker), _signal(symbol=ticker.upper())) is True


def test_literal_symbol_still_filters() -> None:
    assert matches(_rule("AAPL"), _signal(symbol="QBTS")) is False
    assert matches(_rule("AAPL"), _signal(symbol="AAPL")) is True


def test_literal_symbol_case_insensitive() -> None:
    """Users often type 'aapl' — should still match ticker 'AAPL'."""
    assert matches(_rule("aapl"), _signal(symbol="AAPL")) is True


def test_padded_literal_symbol_still_matches() -> None:
    """A pasted `" AAPL "` used to compare untrimmed and never fire."""
    assert normalize_rule_symbol(" aapl ") == "AAPL"
    assert matches(_rule(" AAPL "), _signal(symbol="AAPL")) is True


def test_below_threshold_still_filters_even_with_wildcard_symbol() -> None:
    """Wildcard on symbol must not bypass the metric predicate."""
    assert matches(_rule(None), _signal(composite=15.0)) is False


def test_migration_clears_the_old_any_default() -> None:
    """Rows saved with the old `"ANY"` default become NULL (any ticker); `ALL`
    rows are left alone because they may be Allstate rules."""
    sql = (MIGRATIONS_DIR / "0011_alert_rule_symbols.sql").read_text().upper()
    assert "UPDATE ALERT_RULES" in sql
    assert "'ANY'" in sql
    assert "'ALL'" not in sql
