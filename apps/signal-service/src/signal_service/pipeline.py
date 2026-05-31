"""End-to-end signal pipeline: bars → features → model → score → risk → rationale.

Pure function (no DB writes) so it's easy to test in isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd
from atlas_shared import metrics as mx
from atlas_shared.schemas import AssetClass, Horizon, Signal
from explanation_engine import generate_rationale
from feature_engine import compute_features
from quant_engine.trend import TrendModel
from risk_engine import PortfolioContext, RiskEngine, RiskVeto
from scoring_engine.signal import generate_signal


@dataclass(frozen=True)
class PipelineResult:
    """One of:
      - signal != None and veto is None  → publishable
      - signal == None and veto != None  → killed by risk
      - signal == None and veto is None  → killed by scoring gates
    """

    signal: Signal | None
    veto: RiskVeto | None
    rationale: str | None


def _scrub(features: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in features.items() if not (isinstance(v, float) and math.isnan(v))}


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
    options_features: dict[str, Any] | None = None,
    generate_explanation: bool = True,
) -> PipelineResult:
    if bars.empty or len(bars) < 210:
        mx.SIGNALS_TOTAL.labels(result="insufficient_bars").inc()
        return PipelineResult(None, None, None)

    with mx.time_stage("features"):
        feats_df = compute_features(bars)
    latest = _scrub(feats_df.iloc[-1].to_dict())
    latest["close"] = float(bars["close"].iloc[-1])

    p_up: float | None = None
    model_versions: dict[str, str] = {}
    if trend_model is not None:
        with mx.time_stage("quant"):
            p_up = trend_model.predict_proba(feats_df)
        model_versions["trend"] = trend_model.version

    signal = generate_signal(
        symbol=symbol,
        asset_class=asset_class,
        horizon=horizon,
        features=latest,
        p_up=p_up,
        regime=regime,
        macro_features=macro_features,
        sentiment_features=sentiment_features,
        options_features=options_features,
        model_versions=model_versions,
    )
    if signal is None:
        mx.SIGNALS_TOTAL.labels(result="gated").inc()
        return PipelineResult(None, None, None)

    # Realized vol from features (annualised in compute_features).
    realized_vol = latest.get("vol_realized_20")

    risk = risk_engine or RiskEngine()
    plan = risk.build_plan(
        signal,
        portfolio=portfolio,
        realized_vol_annual=realized_vol if isinstance(realized_vol, (int, float)) else None,
    )
    if isinstance(plan, RiskVeto):
        mx.SIGNALS_TOTAL.labels(result="vetoed").inc()
        return PipelineResult(None, plan, None)

    rationale: str | None = None
    if generate_explanation:
        with mx.time_stage("rationale"):
            rationale = generate_rationale(plan, latest)
        plan = plan.model_copy(update={"rationale_md": rationale})

    mx.SIGNALS_TOTAL.labels(result="published").inc()
    return PipelineResult(plan, None, rationale)
