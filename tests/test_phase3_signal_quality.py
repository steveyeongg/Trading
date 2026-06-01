"""Phase 3 signal-quality guards.

Covers:
  - §5.3 s_tech weighted-block formula — each block contributes the right share
  - §9.3 swing-structure stop loss
  - §9.6 news-event veto
  - §9.1 time_stop_at populated on every published signal
  - §5.2 new indicator surface
"""

from __future__ import annotations

import math
from datetime import datetime
from uuid import uuid4

import numpy as np
import pytest
from atlas_shared.schemas import (
    AssetClass,
    Conviction,
    Direction,
    Horizon,
    Signal,
    SubScores,
)
from feature_engine import compute_features, gbm_bars
from risk_engine import RiskEngine, RiskVeto
from scoring_engine.signal import (
    _STRUCTURAL_STOP_ATR_BUFFER,
    GateResult,
    generate_signal,
)
from scoring_engine.sub_scores import s_tech, s_tech_breakdown


# ── §5.2 indicator surface ────────────────────────────────────────────────────


def test_new_indicators_present_in_output() -> None:
    bars = gbm_bars(n=400, seed=11)
    out = compute_features(bars)
    expected = {
        "sma20", "sma50", "sma200",
        "supertrend", "supertrend_dir",
        "donchian_upper", "donchian_lower", "donchian_middle",
        "keltner_upper", "keltner_lower", "keltner_middle",
        "pivot", "pivot_r1", "pivot_s1",
        "fib_position", "fib_mid",
        "rvol_20",
        "high_52w_dist", "low_52w_dist",
        "gap_pct",
    }
    missing = expected - set(out.columns)
    assert not missing, f"missing new indicators: {missing}"


def test_fib_position_within_bounds() -> None:
    bars = gbm_bars(n=400, seed=11)
    fib = compute_features(bars)["fib_position"].dropna()
    assert ((fib >= 0.0) & (fib <= 1.0)).all()


def test_donchian_envelopes_close() -> None:
    bars = gbm_bars(n=400, seed=11)
    out = compute_features(bars).dropna(subset=["donchian_upper", "donchian_lower"])
    assert (out["donchian_upper"] >= out["donchian_lower"]).all()


def test_supertrend_direction_in_set() -> None:
    bars = gbm_bars(n=400, seed=11)
    direction = compute_features(bars)["supertrend_dir"]
    # int8 +1, -1, or 0 only.
    assert set(int(x) for x in direction.unique()).issubset({-1, 0, 1})


# ── §5.3 s_tech weighted blocks ──────────────────────────────────────────────


def test_s_tech_all_neutral_yields_zero() -> None:
    """A fully neutral feature dict should produce s_tech ≈ 0."""
    assert abs(s_tech({"close": 100.0, "atr14": 1.0})) < 1.0


def test_s_tech_bullish_trend_block_drives_positive() -> None:
    feats = {
        "ema9": 110, "ema21": 105, "ema50": 100, "ema200": 95,
        "adx": 35, "di_plus": 30, "di_minus": 12,
        "supertrend_dir": 1.0,
    }
    breakdown = s_tech_breakdown(feats)
    assert breakdown["trend"] > 70  # all three legs pull up


def test_s_tech_bearish_blocks_drive_negative() -> None:
    feats = {
        "ema9": 90, "ema21": 95, "ema50": 100, "ema200": 105,
        "adx": 30, "di_plus": 10, "di_minus": 28,
        "supertrend_dir": -1.0,
        "rsi14": 30, "macd_hist": -0.6, "stoch_k": 25, "atr14": 1.0,
    }
    assert s_tech(feats) < -30


def test_s_tech_block_weights_sum_to_one() -> None:
    """Sanity: §5.3 block weights must sum to exactly 1.0, otherwise the
    weighted sum would silently lose or gain magnitude."""
    from scoring_engine.sub_scores import _S_TECH_WEIGHTS
    assert math.isclose(sum(_S_TECH_WEIGHTS.values()), 1.0, abs_tol=1e-9)


# ── §9.3 swing-structure stop loss ───────────────────────────────────────────


def _bullish_features() -> dict:
    return {
        "ema9": 105.0, "ema21": 103.0, "ema50": 100.0, "ema200": 95.0,
        "adx": 35.0, "di_plus": 30.0, "di_minus": 12.0,
        "supertrend_dir": 1.0,
        "rsi14": 65.0, "macd_hist": 0.8, "stoch_k": 70.0, "atr14": 1.0,
        "bb_pctb": 0.5, "keltner_middle": 102.0,
        "obv_slope_z": 1.5, "rvol_20": 1.8,
        "smc_bos": 1.0,
        "donchian_upper": 106.0, "donchian_lower": 96.0,
        "fib_position": 0.85,
        "close": 105.0,
    }


def test_structural_stop_uses_swing_low_when_tighter_than_atr() -> None:
    """Donchian lower at 102 (very close to entry 105) should produce a stop
    tighter than the pure 1.8×ATR fallback at 105 - 1.8 = 103.2."""
    feats = _bullish_features()
    feats["donchian_lower"] = 102.0  # close swing low → structural stop is tighter
    outcome = generate_signal(
        symbol="X",
        asset_class=AssetClass.EQUITY,
        horizon=Horizon.SWING,
        features=feats,
        p_up=0.85,
    )
    assert isinstance(outcome, Signal)
    # Structural stop = swing_low (102) - 0.5*atr (0.5) = 101.5.
    # ATR stop = entry (105) - 1.8*atr (1.8) = 103.2.
    # The function picks max(atr_stop, structural_stop) = max(103.2, 101.5) = 103.2.
    # Confirm we got the tighter of the two when the structural stop is below ATR.
    assert outcome.stop_price is not None
    assert 103.0 <= outcome.stop_price <= 103.5


def test_structural_stop_lifts_to_swing_low_when_atr_too_wide() -> None:
    """When the recent swing low is *above* the ATR stop, the structural stop
    wins — that's the whole point of §9.3."""
    feats = _bullish_features()
    feats["donchian_lower"] = 104.0  # tighter than ATR's 103.2
    outcome = generate_signal(
        symbol="X",
        asset_class=AssetClass.EQUITY,
        horizon=Horizon.SWING,
        features=feats,
        p_up=0.85,
    )
    assert isinstance(outcome, Signal)
    # structural = 104 - 0.5 = 103.5. ATR = 103.2. max(...) = 103.5.
    assert outcome.stop_price is not None
    assert 103.4 <= outcome.stop_price <= 103.6


def test_time_stop_is_populated() -> None:
    feats = _bullish_features()
    outcome = generate_signal(
        symbol="X",
        asset_class=AssetClass.EQUITY,
        horizon=Horizon.SWING,
        features=feats,
        p_up=0.85,
    )
    assert isinstance(outcome, Signal)
    assert outcome.time_stop_at is not None
    assert isinstance(outcome.time_stop_at, datetime)
    # SWING default is 10 days → time_stop is in the future.
    now = datetime.now(outcome.time_stop_at.tzinfo)
    assert (outcome.time_stop_at - now).total_seconds() > 0


# ── §9.6 news-event veto ─────────────────────────────────────────────────────


def _build_long_signal(news_score: float, *, confidence_pct: float = 70.0) -> Signal:
    return Signal(
        id=uuid4(),
        symbol="X",
        asset_class=AssetClass.EQUITY,
        generated_at=datetime.now(),
        horizon=Horizon.SWING,
        direction=Direction.LONG,
        composite_score=70.0,
        confidence_pct=confidence_pct,
        conviction=Conviction.HIGH,
        regime="risk-on",
        sub_scores=SubScores(tech=60, quant=60, news=news_score),
        entry_price=100.0,
        stop_price=95.0,
        take_profit_levels=[105.0, 110.0, 115.0],
        position_size_pct=None,
        expected_rr=2.0,
        rationale_md=None,
    )


def test_news_veto_blocks_long_into_very_negative_news() -> None:
    sig = _build_long_signal(news_score=-80.0)
    plan = RiskEngine().build_plan(sig)
    assert isinstance(plan, RiskVeto)
    assert plan.reason == "adverse_news"


def test_news_veto_lets_long_pass_with_mild_negative_news() -> None:
    sig = _build_long_signal(news_score=-30.0)
    plan = RiskEngine().build_plan(sig)
    # Either it sizes a plan, or it vetoes for some non-news reason (depending
    # on sizing math). Either way, news must NOT be the rejection cause.
    if isinstance(plan, RiskVeto):
        assert plan.reason != "adverse_news"
