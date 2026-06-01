"""Regression guard: engines without input data must not bleed weight from
the ones that are actually wired.

BLUEPRINT §8.2 weights:
  tech 0.30, quant 0.25, news 0.10, sent 0.10, macro 0.10, opt 0.05, liq 0.05, risk 0.05.

A bars-only deployment (tech + quant + liq + risk) sums to 0.65 of the
composite weight. Without renormalisation, even a maxed-out tech+quant pair
would cap at 65 — comfortably above the 50 gate, but news/sent/macro/opt
would still steal magnitude. With renormalisation over the active set, the
composite reaches the gate-clearing range cleanly.
"""

from __future__ import annotations

from atlas_shared.schemas import AssetClass, Horizon, SubScores
from scoring_engine.composite import composite
from scoring_engine.signal import GateResult, _build_active, generate_signal


def test_composite_default_behavior_unchanged_when_no_active_set() -> None:
    """No `active` kwarg → behaves exactly like the historical implementation."""
    assert composite(SubScores()) == 0.0
    assert composite(SubScores(tech=50)) == 15.0  # 0.30 * 50
    assert composite(SubScores(quant=80)) == 20.0  # 0.25 * 80


def test_composite_renormalises_over_active_set() -> None:
    """tech+quant maxed, only those two active → renormalised to 1.0."""
    subs = SubScores(tech=100, quant=100)
    # Without renorm: 0.30*100 + 0.25*100 = 55.
    assert composite(subs) == 55.0
    # With renorm over tech+quant: (0.30/0.55)*100 + (0.25/0.55)*100 = 100.
    assert composite(subs, active={"tech", "quant"}) == 100.0


def test_active_set_includes_core_engines_always() -> None:
    active = _build_active(macro_features=None, sentiment_features=None, options_features=None)
    assert active == frozenset({"tech", "quant", "liq", "risk"})


def test_active_set_grows_when_features_supplied() -> None:
    active = _build_active(
        macro_features={"regime": "risk-on"},
        sentiment_features={"news_sent_score": 0.4, "news_count": 3},
        options_features=None,
        news_features={"news_sent_score": 0.4, "news_count": 3},
    )
    assert active == frozenset({"tech", "quant", "liq", "risk", "macro", "sent", "news"})


def test_news_excluded_when_count_zero() -> None:
    """A news_features dict with zero headlines must NOT activate the news
    engine — otherwise the composite would dilute itself with structural
    zeros from an engine that has no signal to contribute."""
    active = _build_active(
        macro_features=None,
        sentiment_features=None,
        options_features=None,
        news_features={"news_sent_score": 0.0, "news_count": 0.0},
    )
    assert "news" not in active


def test_bars_only_pipeline_can_produce_a_signal() -> None:
    """End-to-end: a strongly bullish feature set across all §5.3 blocks
    (trend / momentum / volume / structure) must produce a signal even with
    no macro / sent / news engines wired."""
    features = {
        # Trend block — EMA stack aligned, ADX with DI+ dominance, supertrend up.
        "ema9": 105.0, "ema21": 103.0, "ema50": 100.0, "ema200": 95.0,
        "adx": 35.0, "di_plus": 30.0, "di_minus": 12.0,
        "supertrend_dir": 1.0,
        # Momentum block.
        "rsi14": 65.0, "macd_hist": 0.8, "stoch_k": 70.0,
        # Volatility block.
        "bb_pctb": 0.5, "keltner_middle": 102.0,
        # Volume block — strong OBV slope + above-average relative volume.
        "obv_slope_z": 1.5, "rvol_20": 1.8,
        # Structure block — break-of-structure + Donchian breakout near upper band.
        "smc_bos": 1.0, "divergence_bull": 0.0, "divergence_bear": 0.0,
        "donchian_upper": 106.0, "donchian_lower": 96.0,
        "fib_position": 0.85,
        # Risk block inputs.
        "atr14": 1.0, "close": 105.0,
    }
    outcome = generate_signal(
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        horizon=Horizon.SWING,
        features=features,
        p_up=0.85,
        regime="unknown",
        macro_features=None,
        sentiment_features=None,
        options_features=None,
    )
    assert not isinstance(outcome, GateResult), (
        f"bars-only with strong tech+quant must fire — got gate reason: "
        f"{outcome.reason if isinstance(outcome, GateResult) else ''}"
    )
    assert outcome.direction.value == "long"
    assert outcome.composite_score >= 50.0
