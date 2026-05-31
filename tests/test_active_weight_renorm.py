"""Regression guard: deferred sub-scores (`fund`, `chain`) must not bleed
weight from the ones that are actually wired.

The bug this prevents: with the blueprint default weights, fund(0.15) +
opt(0.10) + chain(0.05) = 0.30 of the composite weight points at sub-scores
that have no adapter on the production route. That capped achievable
composite at 0.70 × 100 = 70 even in the best case, and at 0.45 × 100 = 45
for bars-only deployments (tech+quant only) — which can never clear the ≥50
gate. Result: every signal returns null even with healthy data.

The fix renormalises weights over the active set so the gate stays
unreachable for *real* neutrality, not for missing adapters.
"""

from __future__ import annotations

from atlas_shared.schemas import SubScores
from scoring_engine.composite import composite
from scoring_engine.signal import _build_active, generate_signal
from atlas_shared.schemas import AssetClass, Horizon


def test_composite_default_behavior_unchanged_when_no_active_set() -> None:
    """No `active` kwarg → behaves exactly like the historical implementation."""
    assert composite(SubScores()) == 0.0
    assert composite(SubScores(tech=50)) == 10.0
    assert composite(SubScores(quant=80)) == 20.0


def test_composite_renormalises_over_active_set() -> None:
    """tech+quant maxed, only those two active → renormalised to 1.0
    so the composite reaches the gate-clearing range."""
    subs = SubScores(tech=100, quant=100)  # fund/macro/etc all 0
    # Without renorm: 0.20×100 + 0.25×100 = 45 (cannot clear ≥50 gate).
    assert composite(subs) == 45.0
    # With renorm over tech+quant: (0.20/0.45)×100 + (0.25/0.45)×100 = 100.
    assert composite(subs, active={"tech", "quant"}) == 100.0


def test_active_set_includes_liq_always_excludes_deferred() -> None:
    active = _build_active(macro_features=None, sentiment_features=None, options_features=None)
    assert active == frozenset({"tech", "quant", "liq"})
    assert "fund" not in active
    assert "chain" not in active


def test_active_set_grows_when_features_supplied() -> None:
    active = _build_active(
        macro_features={"regime": "risk-on"},
        sentiment_features={"news_sent_score": 0.4, "news_count": 3},
        options_features=None,
    )
    assert active == frozenset({"tech", "quant", "liq", "macro", "sent"})


def test_bars_only_pipeline_can_now_produce_a_signal() -> None:
    """End-to-end: a strongly bullish tech+quant set with no other inputs
    must produce a signal (previously gated permanently at composite=45)."""
    features = {
        # Strongly bullish technicals — saturates s_tech near +100.
        "rsi14": 65.0, "macd_hist": 0.8, "atr14": 1.0,
        "ema9": 105.0, "ema21": 103.0, "ema50": 100.0, "ema200": 95.0,
        "bb_pctb": 0.5, "adx": 35.0, "obv_slope_z": 1.5,
        "smc_bos": 1.0, "divergence_bull": 0.0, "divergence_bear": 0.0,
        "close": 105.0,
    }
    sig = generate_signal(
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        horizon=Horizon.SWING,
        features=features,
        p_up=0.85,  # strongly bullish quant
        regime="unknown",          # no macro signal
        macro_features=None,       # no macro adapter wired
        sentiment_features=None,   # no news
        options_features=None,     # not wired in production route
    )
    assert sig is not None, "bars-only with strong tech+quant must now fire a signal"
    assert sig.direction.value == "long"
    assert sig.composite_score >= 50.0
