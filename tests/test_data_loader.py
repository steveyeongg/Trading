"""Postgres data loader — verifies the wiring without requiring a live DB.

We don't run Postgres in CI. These tests prove the public API exists with
the right shape and that the empty-DB path is handled gracefully.
"""

from __future__ import annotations

import pytest
from quant_engine.data import load_bars_for_symbols, load_features_for_training


def test_loader_empty_symbols_returns_empty() -> None:
    assert load_bars_for_symbols([]) == {}


def test_loader_features_empty_returns_empty_frame() -> None:
    assert load_features_for_training([]).empty


@pytest.mark.skipif(True, reason="requires running Postgres; manual smoke test")
def test_loader_against_live_db() -> None:
    """Manual integration: pre-load bars with `ingest_equities synthetic`,
    then run this. Left here as a docstring of the expected shape."""
    out = load_bars_for_symbols(["AAPL"], resolution="1m")
    assert "AAPL" in out
    assert "ts" in out["AAPL"].columns
