"""Screener — universe resolution + run shaping.

The HTTP route hits Postgres so isn't covered here; this exercises the pure
pieces in `signal_service.screener` against the real `infra/data/universes.json`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from signal_service.screener import (
    list_universes,
    load_universes,
    resolve_symbols,
)


@pytest.fixture(autouse=True)
def _reset_universe_cache() -> None:
    """`load_universes` is lru_cached; clear between tests so env overrides land."""
    load_universes.cache_clear()
    yield
    load_universes.cache_clear()


def test_default_universe_file_loads() -> None:
    universes = load_universes()
    assert "default_watchlist" in universes
    syms = universes["default_watchlist"]["symbols"]
    assert "AAPL" in syms and "SPY" in syms


def test_list_universes_returns_metadata() -> None:
    items = list_universes()
    assert all({"key", "label", "description", "symbol_count"} <= set(item) for item in items)
    # Symbol counts are non-zero and stable.
    by_key = {it["key"]: it for it in items}
    assert by_key["default_watchlist"]["symbol_count"] >= 6


def test_manual_mode_uses_passed_symbols() -> None:
    out = resolve_symbols("manual", ["aapl", "nvda", " tsla "])
    assert out == ["AAPL", "NVDA", "TSLA"]


def test_manual_mode_empty_falls_back_to_default() -> None:
    """BLUEPRINT §4.3 — empty manual list should not return an empty scan;
    fall back to the default watchlist so the user always gets results."""
    out = resolve_symbols("manual", [])
    default = load_universes()["default_watchlist"]["symbols"]
    assert out == default


def test_universe_mode_loads_symbols() -> None:
    out = resolve_symbols("us_megacap", [])
    assert "AAPL" in out and len(out) >= 20


def test_universe_mode_appends_manual_symbols_deduped() -> None:
    """A user can extend a named universe with one-off tickers without
    losing dedup."""
    out = resolve_symbols("default_watchlist", ["XYZ", "AAPL"])  # AAPL already present
    assert out.count("AAPL") == 1
    assert "XYZ" in out


def test_unknown_universe_raises() -> None:
    with pytest.raises(ValueError, match="unknown universe"):
        resolve_symbols("does_not_exist", [])


def test_env_override_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ATLAS_UNIVERSES_PATH lets a deployment swap the universe file."""
    p = tmp_path / "u.json"
    p.write_text(json.dumps({
        "tiny": {"label": "Tiny", "description": "", "symbols": ["FOO", "BAR"]},
    }))
    monkeypatch.setenv("ATLAS_UNIVERSES_PATH", str(p))
    load_universes.cache_clear()
    universes = load_universes()
    assert list(universes.keys()) == ["tiny"]
    assert resolve_symbols("tiny", []) == ["FOO", "BAR"]


def test_missing_file_falls_back_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_UNIVERSES_PATH", str(tmp_path / "missing.json"))
    load_universes.cache_clear()
    universes = load_universes()
    assert "default_watchlist" in universes  # embedded fallback
