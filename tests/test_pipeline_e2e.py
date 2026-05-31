"""End-to-end pipeline test: synthetic bars → features → trend model → risk → rationale.

Skips the DB-backed API routes (those need Postgres). Proves the pure
compute chain wires together cleanly.
"""

from __future__ import annotations

import pandas as pd
import pytest
from feature_engine import compute_features, gbm_bars
from quant_engine import binary_up_labels, train_trend, triple_barrier_labels
from quant_engine.trend import TrendModel, save
from signal_service.pipeline import PipelineResult, run_pipeline


@pytest.fixture(scope="module")
def trained_model(tmp_path_factory) -> TrendModel:
    bars = gbm_bars(n=2500, seed=7)
    feats = compute_features(bars)
    labels = binary_up_labels(triple_barrier_labels(feats["close"], feats["atr14"]))
    art = train_trend(feats, labels, version="test-e2e", n_splits=4, test_size=250)
    p = save(art, tmp_path_factory.mktemp("reg") / "model.joblib")
    return TrendModel.load(p)


def test_pipeline_returns_result_object(trained_model: TrendModel) -> None:
    bars = gbm_bars(n=600, seed=21)
    result = run_pipeline(bars=bars, symbol="SYN", trend_model=trained_model, generate_explanation=False)
    assert isinstance(result, PipelineResult)
    # Three valid terminal states: published, vetoed, scoring-killed.
    if result.signal is not None:
        assert result.veto is None
        assert result.signal.symbol == "SYN"
        assert -100 <= result.signal.composite_score <= 100
        assert 0 <= result.signal.confidence_pct <= 100
        assert result.signal.direction.value in {"long", "short"}
        assert "trend" in result.signal.model_versions
        # If a signal made it through risk, it must be sized.
        assert result.signal.position_size_pct is not None
        assert result.signal.position_size_pct > 0
    elif result.veto is not None:
        assert result.veto.reason  # explanation present


def test_pipeline_short_history_returns_empty(trained_model: TrendModel) -> None:
    bars = gbm_bars(n=50, seed=1)
    result = run_pipeline(bars=bars, symbol="SHORT", trend_model=trained_model, generate_explanation=False)
    assert result.signal is None
    assert result.veto is None


def test_pipeline_without_model(trained_model: TrendModel) -> None:
    """Without a model the technical path still runs; risk gate may veto it."""
    bars = gbm_bars(n=600, seed=99)
    result = run_pipeline(bars=bars, symbol="NOML", trend_model=None, generate_explanation=False)
    # Confidence comes from |composite|/100 without a model; risk gate
    # typically vetos this on `low_confidence`.
    if result.veto:
        assert result.veto.reason in {"low_confidence", "low_rr", "size_too_small"}


def test_pipeline_empty_bars() -> None:
    result = run_pipeline(bars=pd.DataFrame(), symbol="EMPTY", generate_explanation=False)
    assert result.signal is None and result.veto is None


def test_pipeline_passes_signal_through_risk_to_rationale(trained_model: TrendModel) -> None:
    """When the gates do pass, the published Signal carries a rendered
    rationale. We don't depend on synthetic data clearing the gates — we
    inspect the wiring: a non-None signal always has a non-None rationale
    when generate_explanation=True."""
    for seed in range(60):
        bars = gbm_bars(n=600, seed=seed)
        result = run_pipeline(
            bars=bars,
            symbol="RAT",
            trend_model=trained_model,
            generate_explanation=True,
        )
        if result.signal is not None:
            assert result.signal.rationale_md is not None
            assert "SIGNAL: RAT" in result.signal.rationale_md
            return
    # If no synthetic seed published, the pipeline still ran without errors —
    # which is what wiring tests need to verify.
    pytest.skip("no seed produced a published signal; wiring path uncovered")
