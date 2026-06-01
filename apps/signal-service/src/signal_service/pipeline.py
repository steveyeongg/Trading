"""End-to-end signal pipeline: bars → features → model → score → risk → rationale.

Pure function (no DB writes) so it's easy to test in isolation. Every terminal
state carries a reason — `no_signal_reason` for scoring gates, `veto.reason`
for risk vetoes — so the dashboard can show *why* a candidate didn't publish.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from atlas_shared import metrics as mx
from atlas_shared.schemas import AssetClass, Horizon, Signal
from explanation_engine import generate_rationale
from feature_engine import compute_features
from quant_engine.trend import TrendModel
from risk_engine import PortfolioContext, RiskEngine, RiskVeto
from scoring_engine.signal import GateResult, generate_signal

# BLUEPRINT §9.6 + §20.1 — refuse to trade on bars older than the horizon
# tolerates. Env-overridable. Values in hours.
_MAX_BAR_AGE_HOURS: dict[Horizon, float] = {
    Horizon.INTRADAY: float(os.environ.get("ATLAS_MAX_BAR_AGE_INTRADAY_H", "2")),
    Horizon.SWING: float(os.environ.get("ATLAS_MAX_BAR_AGE_SWING_H", "30")),
    Horizon.POSITION: float(os.environ.get("ATLAS_MAX_BAR_AGE_POSITION_H", "96")),
    Horizon.LONG_TERM: float(os.environ.get("ATLAS_MAX_BAR_AGE_LONG_TERM_H", "168")),
}

_MIN_BARS = 210


@dataclass(frozen=True)
class PipelineResult:
    """One of:
      - signal != None and veto is None       → publishable
      - signal == None and veto != None       → killed by risk engine
      - signal == None and no_signal_reason   → killed by scoring gate / stale data / insufficient bars
    """

    signal: Signal | None
    veto: RiskVeto | None
    rationale: str | None
    no_signal_reason: str | None = None


def _scrub(features: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in features.items() if not (isinstance(v, float) and math.isnan(v))}


def _bar_age_seconds(bars: pd.DataFrame) -> float | None:
    ts = bars["ts"].iloc[-1] if "ts" in bars.columns and len(bars) else None
    if ts is None:
        return None
    if not isinstance(ts, datetime):
        ts = pd.Timestamp(ts).to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds()


def run_pipeline(
    bars: pd.DataFrame,
    symbol: str,
    horizon: Horizon = Horizon.SWING,
    asset_class: AssetClass = AssetClass.EQUITY,
    trend_model: TrendModel | None = None,
    risk_engine: RiskEngine | None = None,
    portfolio: PortfolioContext | None = None,
    regime: str = "unknown",
    macro_features: dict[str, Any] | None = None,
    sentiment_features: dict[str, Any] | None = None,
    news_features: dict[str, Any] | None = None,
    options_features: dict[str, Any] | None = None,
    generate_explanation: bool = True,
) -> PipelineResult:
    if bars.empty or len(bars) < _MIN_BARS:
        mx.SIGNALS_TOTAL.labels(result="insufficient_bars").inc()
        return PipelineResult(
            None, None, None,
            no_signal_reason=f"insufficient bars: have {len(bars)}, need ≥{_MIN_BARS}",
        )

    # BLUEPRINT §9.6 stale-data veto. Bars from synthetic test fixtures may be
    # ahead of "now" — only veto when bars are actually older than the limit.
    bar_age = _bar_age_seconds(bars)
    max_age_h = _MAX_BAR_AGE_HOURS.get(horizon, 30.0)
    if bar_age is not None and bar_age > max_age_h * 3600:
        mx.SIGNALS_TOTAL.labels(result="stale_data").inc()
        return PipelineResult(
            None, None, None,
            no_signal_reason=(
                f"stale data: last bar is {bar_age / 3600:.1f}h old, "
                f"horizon {horizon.value} allows ≤{max_age_h:.0f}h"
            ),
        )

    with mx.time_stage("features"):
        feats_df = compute_features(bars)
    latest = _scrub(feats_df.iloc[-1].to_dict())
    latest["close"] = float(bars["close"].iloc[-1])

    p_up: float | None = None
    quant_meta: dict = {}
    model_versions: dict[str, str] = {}
    if trend_model is not None:
        with mx.time_stage("quant"):
            quant_meta = trend_model.predict_full(feats_df)
        p_up = float(quant_meta["p_up"])
        model_versions["trend"] = trend_model.version

    outcome = generate_signal(
        symbol=symbol,
        asset_class=asset_class,
        horizon=horizon,
        features=latest,
        p_up=p_up,
        regime=regime,
        macro_features=macro_features,
        sentiment_features=sentiment_features,
        news_features=news_features,
        options_features=options_features,
        model_versions=model_versions,
        bar_age_seconds=bar_age,
        quant_meta=quant_meta,
    )
    if isinstance(outcome, GateResult):
        mx.SIGNALS_TOTAL.labels(result="gated").inc()
        return PipelineResult(None, None, None, no_signal_reason=outcome.reason)

    signal = outcome

    realized_vol = latest.get("vol_realized_20")
    risk = risk_engine or RiskEngine()
    plan = risk.build_plan(
        signal,
        portfolio=portfolio,
        realized_vol_annual=realized_vol if isinstance(realized_vol, (int, float)) else None,
    )
    if isinstance(plan, RiskVeto):
        mx.SIGNALS_TOTAL.labels(result="vetoed").inc()
        return PipelineResult(None, plan, None, no_signal_reason=f"risk veto: {plan.reason}")

    rationale: str | None = None
    if generate_explanation:
        with mx.time_stage("rationale"):
            rationale = generate_rationale(plan, latest)
        plan = plan.model_copy(update={"rationale_md": rationale})

    mx.SIGNALS_TOTAL.labels(result="published").inc()
    return PipelineResult(plan, None, rationale)
