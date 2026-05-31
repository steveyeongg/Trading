"""Signal generator — composes sub-scores into a Signal.

This is the deterministic core of the engine. Risk gates, multi-engine
confirmation gates, devil's-advocate counter-case, and the LLM rationale layer
are Phase 1.5+.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from atlas_shared.schemas import (
    AssetClass,
    Conviction,
    Direction,
    Horizon,
    Signal,
    SubScores,
)
from macro_engine import s_macro
from options_analytics import s_options
from sentiment_engine import s_sent

from scoring_engine.composite import DEFAULT_WEIGHTS, composite
from scoring_engine.sub_scores import s_liq, s_quant, s_tech

# Minimum gates — BLUEPRINT §9 step 4. Tunable per asset class / horizon.
# All four are env-overridable so a Phase 1.5 deployment with sparse data
# coverage can loosen them without code changes:
#   ATLAS_MIN_COMPOSITE, ATLAS_MIN_CONFIDENCE,
#   ATLAS_MIN_CONFIRMING_ENGINES, ATLAS_MIN_AGREE_THRESHOLD
import os as _os

MIN_COMPOSITE = float(_os.environ.get("ATLAS_MIN_COMPOSITE", "50.0"))
MIN_CONFIDENCE = float(_os.environ.get("ATLAS_MIN_CONFIDENCE", "0.55"))
MIN_CONFIRMING_ENGINES = int(_os.environ.get("ATLAS_MIN_CONFIRMING_ENGINES", "2"))
MIN_AGREE_THRESHOLD = float(_os.environ.get("ATLAS_MIN_AGREE_THRESHOLD", "40.0"))

# Sub-scores whose adapter is always available (always counted in the active
# set). `fund` and `chain` are deferred — no adapter exists, the score would
# always be 0, and including them would silently steal weight from the engines
# that ARE wired. `macro`, `sent`, `opt` are conditional — included when their
# feature dict is supplied.
_ALWAYS_ACTIVE: frozenset[str] = frozenset({"tech", "quant", "liq"})


def _conviction(composite_abs: float, confidence: float) -> Conviction:
    if composite_abs >= 80 and confidence >= 0.75:
        return Conviction.VERY_HIGH
    if composite_abs >= 70 and confidence >= 0.65:
        return Conviction.HIGH
    if composite_abs >= 60:
        return Conviction.MEDIUM
    return Conviction.LOW


def _engines_agree(
    subs: SubScores, direction: Direction, thresh: float, active: frozenset[str]
) -> int:
    """Count engines in the active set whose score leans `>= thresh` in the
    signal direction. Inactive engines (deferred / not-wired adapters) are
    skipped — they would always be 0 and would bias the count toward `gated`."""
    sign = 1 if direction == Direction.LONG else -1
    return sum(1 for k in active if sign * getattr(subs, k) >= thresh)


def _build_active(
    macro_features: dict | None,
    sentiment_features: dict | None,
    options_features: dict | None,
) -> frozenset[str]:
    """Active set = always-on engines + any optional engine whose feature dict
    was supplied by the caller. Deferred adapters (fund, chain) are never
    included until they're built."""
    active = set(_ALWAYS_ACTIVE)
    if macro_features:
        active.add("macro")
    if sentiment_features:
        active.add("sent")
    if options_features:
        active.add("opt")
    return frozenset(active)


def generate_signal(
    symbol: str,
    asset_class: AssetClass,
    horizon: Horizon,
    features: dict[str, Any],
    p_up: float | None = None,
    regime: str = "unknown",
    macro_features: dict[str, Any] | None = None,
    sentiment_features: dict[str, Any] | None = None,
    options_features: dict[str, Any] | None = None,
    model_versions: dict[str, str] | None = None,
    weights: dict[str, float] = DEFAULT_WEIGHTS,
) -> Signal | None:
    """Return a Signal if all gates pass, else None.

    `features` is a flat dict of the latest feature vector (see
    feature_engine.latest_feature_row). `macro_features`, `sentiment_features`,
    and `options_features` are optional flat dicts; missing → 0 (neutral).
    """
    macro_in = dict(macro_features or {})
    macro_in.setdefault("regime", regime)
    subs = SubScores(
        tech=s_tech(features),
        quant=s_quant(p_up),
        liq=s_liq(features),
        macro=s_macro(macro_in),
        sent=s_sent(sentiment_features or {}),
        opt=s_options(options_features or {}),
    )
    # Only count engines that are actually wired. `fund` and `chain` are
    # deferred — including them would steal 0.20 of the weight and make the
    # composite mathematically unable to clear 50 on bars-only deployments.
    active = _build_active(macro_features, sentiment_features, options_features)
    score = composite(subs, weights=weights, active=active)
    if abs(score) < MIN_COMPOSITE:
        return None

    direction = Direction.LONG if score > 0 else Direction.SHORT

    if _engines_agree(subs, direction, MIN_AGREE_THRESHOLD, active) < MIN_CONFIRMING_ENGINES:
        return None

    # Confidence: until we have a calibrated meta-model, derive from |p_up - 0.5|.
    if p_up is not None:
        confidence = float(min(1.0, abs(p_up - 0.5) * 2.0))
    else:
        confidence = min(1.0, abs(score) / 100.0)
    if confidence < MIN_CONFIDENCE and p_up is not None:
        return None

    close = features.get("close")
    atr = features.get("atr14")
    entry = float(close) if close is not None else None
    if entry is not None and atr is not None:
        # ATR-stop, 1.8x. Targets ladder 1R / 2R / 3R per blueprint §10.2.
        risk = 1.8 * float(atr)
        if direction == Direction.LONG:
            stop = entry - risk
            targets = [entry + risk, entry + 2 * risk, entry + 3 * risk]
        else:
            stop = entry + risk
            targets = [entry - risk, entry - 2 * risk, entry - 3 * risk]
        rr = 2.0
    else:
        stop = None
        targets = []
        rr = None

    return Signal(
        id=uuid4(),
        symbol=symbol,
        asset_class=asset_class,
        generated_at=datetime.now(UTC),
        horizon=horizon,
        direction=direction,
        composite_score=round(score, 2),
        confidence_pct=round(confidence * 100.0, 2),
        conviction=_conviction(abs(score), confidence),
        regime=regime,
        sub_scores=subs,
        entry_price=entry,
        stop_price=stop,
        take_profit_levels=targets,
        position_size_pct=None,  # risk engine fills this in Phase 1.5
        expected_rr=rr,
        rationale_md=None,
        model_versions=model_versions or {},
    )
