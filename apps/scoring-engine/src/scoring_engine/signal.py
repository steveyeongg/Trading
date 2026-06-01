"""Signal generator — composes sub-scores into a Signal.

This is the deterministic core of the engine. When gates fail, returns a
`GateResult` carrying a human-readable reason so the pipeline can surface it
to the dashboard ("no signal: composite 42 < 50") instead of returning a
silent None.
"""

from __future__ import annotations

import os as _os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from scoring_engine.sub_scores import s_liq, s_news, s_quant, s_risk, s_tech

# Minimum gates — BLUEPRINT §8.5. All env-overridable so deployments can loosen
# them without code changes:
#   ATLAS_MIN_COMPOSITE, ATLAS_MIN_CONFIDENCE,
#   ATLAS_MIN_CONFIRMING_ENGINES, ATLAS_MIN_AGREE_THRESHOLD
MIN_COMPOSITE = float(_os.environ.get("ATLAS_MIN_COMPOSITE", "50.0"))
MIN_CONFIDENCE = float(_os.environ.get("ATLAS_MIN_CONFIDENCE", "0.55"))
MIN_CONFIRMING_ENGINES = int(_os.environ.get("ATLAS_MIN_CONFIRMING_ENGINES", "2"))
MIN_AGREE_THRESHOLD = float(_os.environ.get("ATLAS_MIN_AGREE_THRESHOLD", "40.0"))

# Sub-scores whose adapter is always available. Conditional engines (macro,
# sent, opt, news) are added by `_build_active` only when their feature dict
# is supplied — keeps the composite from auto-zeroing on missing inputs.
_ALWAYS_ACTIVE: frozenset[str] = frozenset({"tech", "quant", "liq", "risk"})

# Horizon-dependent time stop (bars equivalent; converted from days × hours).
# BLUEPRINT §9.1 — `time_stop` is recommended for every signal.
_TIME_STOP_HOURS: dict[Horizon, float] = {
    Horizon.INTRADAY: 6.5,         # one US trading session
    Horizon.SWING: 10 * 24,        # ~10 calendar days
    Horizon.POSITION: 60 * 24,     # ~60 days
    Horizon.LONG_TERM: 252 * 24,   # ~1 trading year
}

# Buffer added to the swing low/high before using it as a structural stop.
# A few ATR ticks below the recent low — gives the trade room to breathe
# without sitting right on the obvious level where stop-hunts happen.
_STRUCTURAL_STOP_ATR_BUFFER: float = 0.5


@dataclass(frozen=True)
class GateResult:
    """Returned when scoring gates kill a candidate. Carries the reason so
    the dashboard can show *why* there's no signal."""

    reason: str
    sub_scores: SubScores
    composite_score: float
    direction_implied: Direction | None
    confidence: float
    active_engines: frozenset[str]


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
    sign = 1 if direction == Direction.LONG else -1
    return sum(1 for k in active if sign * getattr(subs, k) >= thresh)


def _build_active(
    macro_features: dict | None,
    sentiment_features: dict | None,
    options_features: dict | None,
    news_features: dict | None = None,
) -> frozenset[str]:
    """Active set = always-on engines + any optional engine whose feature dict
    was supplied. Engines without input data are excluded so they don't dilute
    the composite via renormalisation."""
    active = set(_ALWAYS_ACTIVE)
    if macro_features:
        active.add("macro")
    if sentiment_features:
        active.add("sent")
    if options_features:
        active.add("opt")
    if news_features and float(news_features.get("news_count", 0.0)) > 0:
        active.add("news")
    return frozenset(active)


def _invalidations(direction: Direction, stop: float | None) -> list[str]:
    """BLUEPRINT §9.1 — every signal must declare the conditions that kill it."""
    out: list[str] = []
    if stop is not None:
        out.append(
            f"Close {'below' if direction == Direction.LONG else 'above'} {stop:.2f} (stop loss)"
        )
    out.append("Macro regime flips against direction")
    out.append("High-impact adverse news event")
    return out


def generate_signal(
    symbol: str,
    asset_class: AssetClass,
    horizon: Horizon,
    features: dict[str, Any],
    p_up: float | None = None,
    regime: str = "unknown",
    macro_features: dict[str, Any] | None = None,
    sentiment_features: dict[str, Any] | None = None,
    news_features: dict[str, Any] | None = None,
    options_features: dict[str, Any] | None = None,
    model_versions: dict[str, str] | None = None,
    weights: dict[str, float] = DEFAULT_WEIGHTS,
    bar_age_seconds: float | None = None,
    quant_meta: dict | None = None,
) -> Signal | GateResult:
    """Return a `Signal` if all gates pass, else a `GateResult` carrying the
    reason. Never returns None — callers always learn *why* a candidate failed.
    """
    macro_in = dict(macro_features or {})
    macro_in.setdefault("regime", regime)

    # `news_features` and `sentiment_features` may be the same dict for now
    # (both derived from `recent_sentiment_features`). The split is structural
    # so social/analyst sources can land in `sentiment_features` later without
    # touching the news scoring path.
    news_in = news_features if news_features is not None else sentiment_features

    subs = SubScores(
        tech=s_tech(features),
        quant=s_quant(p_up),
        liq=s_liq(features),
        risk=s_risk(features),
        macro=s_macro(macro_in),
        sent=s_sent(sentiment_features or {}),
        opt=s_options(options_features or {}),
        news=s_news(news_in),
    )

    active = _build_active(macro_features, sentiment_features, options_features, news_in)
    score = composite(subs, weights=weights, active=active)
    direction = Direction.LONG if score >= 0 else Direction.SHORT
    confirming = _engines_agree(subs, direction, MIN_AGREE_THRESHOLD, active)

    if p_up is not None:
        confidence = float(min(1.0, abs(p_up - 0.5) * 2.0))
    else:
        confidence = min(1.0, abs(score) / 100.0)

    def _gate_failure(reason: str) -> GateResult:
        return GateResult(
            reason=reason,
            sub_scores=subs,
            composite_score=round(score, 2),
            direction_implied=direction,
            confidence=round(confidence, 3),
            active_engines=active,
        )

    if abs(score) < MIN_COMPOSITE:
        return _gate_failure(
            f"composite |{score:.1f}| < {MIN_COMPOSITE:.0f} (need ≥{MIN_COMPOSITE:.0f})"
        )

    if confirming < MIN_CONFIRMING_ENGINES:
        return _gate_failure(
            f"only {confirming} engine(s) confirm {direction.value} at ≥{MIN_AGREE_THRESHOLD:.0f} "
            f"(need ≥{MIN_CONFIRMING_ENGINES})"
        )

    if p_up is not None and confidence < MIN_CONFIDENCE:
        return _gate_failure(
            f"confidence {confidence:.2f} < {MIN_CONFIDENCE:.2f}"
        )

    close = features.get("close")
    atr = features.get("atr14")
    swing_low = features.get("donchian_lower")
    swing_high = features.get("donchian_upper")
    entry = float(close) if close is not None else None

    if entry is not None and atr is not None:
        atr_f = float(atr)
        atr_stop_dist = 1.8 * atr_f
        buffer = _STRUCTURAL_STOP_ATR_BUFFER * atr_f

        if direction == Direction.LONG:
            atr_stop = entry - atr_stop_dist
            structural_stop = (
                float(swing_low) - buffer if swing_low is not None else atr_stop
            )
            # BLUEPRINT §9.3 — pick the *higher* (tighter) of the two for a long
            # so the trade isn't running with an oversized leash; never let
            # structure go above entry (sanity guard).
            stop = max(atr_stop, structural_stop)
            stop = min(stop, entry - 0.25 * atr_f)
            risk_per_share = max(entry - stop, 0.25 * atr_f)
            targets = [
                entry + risk_per_share,
                entry + 2 * risk_per_share,
                entry + 3 * risk_per_share,
            ]
        else:
            atr_stop = entry + atr_stop_dist
            structural_stop = (
                float(swing_high) + buffer if swing_high is not None else atr_stop
            )
            stop = min(atr_stop, structural_stop)
            stop = max(stop, entry + 0.25 * atr_f)
            risk_per_share = max(stop - entry, 0.25 * atr_f)
            targets = [
                entry - risk_per_share,
                entry - 2 * risk_per_share,
                entry - 3 * risk_per_share,
            ]
        rr = 2.0
    else:
        stop = None
        targets = []
        rr = None

    # BLUEPRINT §9.1 — every signal carries a time stop. If price hasn't
    # progressed toward T1 by then, the thesis is stale.
    time_stop_at = datetime.now(UTC) + timedelta(hours=_TIME_STOP_HOURS.get(horizon, 240.0))

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
        position_size_pct=None,
        expected_rr=rr,
        rationale_md=None,
        invalidations=_invalidations(direction, stop),
        model_versions=model_versions or {},
        bar_age_seconds=bar_age_seconds,
        time_stop_at=time_stop_at,
        quant_meta=quant_meta or {},
    )
