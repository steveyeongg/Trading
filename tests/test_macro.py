"""Macro engine: FRED stub, regime classifier, s_macro sub-score."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from macro_engine import FredClient, RegimeClassifier, s_macro
from macro_engine.fred import _synthetic_series


def test_synthetic_series_deterministic() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 6, 1, tzinfo=UTC)
    a = _synthetic_series("VIXCLS", start, end)
    b = _synthetic_series("VIXCLS", start, end)
    assert a.equals(b)
    # Plausible VIX levels: synthetic is a random walk around 16, so we accept ±20 swings.
    assert 1.0 < a.median() < 50.0


def test_fred_client_falls_back_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    async def _go() -> pd.Series:
        async with FredClient() as c:
            assert not c.available
            return await c.series("VIXCLS", days=120)

    s = asyncio.run(_go())
    assert not s.empty
    assert s.name == "VIXCLS"


def test_regime_classifier_returns_known_label() -> None:
    rng_start = datetime(2024, 1, 1, tzinfo=UTC)
    rng_end = rng_start + timedelta(days=400)
    series = {sid: _synthetic_series(sid, rng_start, rng_end) for sid in ("T10Y2Y", "VIXCLS", "DTWEXBGS", "DGS10", "DGS2", "CPIAUCSL")}
    clf = RegimeClassifier().fit(series)
    out = clf.classify(series)
    assert out.label in {"risk-on", "risk-off", "late-cycle", "stagflation"}
    assert 0.0 <= out.confidence <= 1.0
    assert abs(sum(out.probabilities.values()) - 1.0) < 1e-6


def test_regime_classifier_unknown_without_data() -> None:
    clf = RegimeClassifier()
    out = clf.classify({})
    assert out.label == "unknown"


def test_s_macro_neutral_on_empty() -> None:
    assert s_macro({}) == 0.0


def test_s_macro_bearish_when_risk_off_and_high_vix() -> None:
    score = s_macro({
        "regime": "risk-off",
        "regime_confidence": 0.8,
        "vix": 35.0,
        "vix_z": 2.5,
        "term_spread": -0.4,
        "dxy_z": 1.0,
    })
    assert score < -20.0


def test_s_macro_bullish_when_risk_on_and_low_vol() -> None:
    score = s_macro({
        "regime": "risk-on",
        "regime_confidence": 0.85,
        "vix": 14.0,
        "vix_z": -1.0,
        "term_spread": 0.5,
        "dxy_z": -0.5,
    })
    assert score > 20.0


def test_s_macro_vix_brake_kicks_in() -> None:
    """VIX > 30 caps the score at 0 even when other inputs are bullish."""
    score = s_macro({
        "regime": "risk-on",
        "regime_confidence": 0.9,
        "vix": 40.0,
        "vix_z": -1.0,
        "term_spread": 1.0,
    })
    assert score <= 0.0
