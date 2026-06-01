"""ATLAS production strategy adapter for backtesting.

Runs the live signal pipeline (feature compute → quant inference → composite
score → risk gate) on each bar, surfaces orders when the gates pass.

We deliberately wrap the production code rather than re-implementing the
logic — backtest divergence from prod is the #1 source of fake alpha.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
from atlas_shared.schemas import AssetClass, Direction, Horizon
from feature_engine import compute_features
from risk_engine import RiskEngine, RiskVeto
from scoring_engine.signal import GateResult, generate_signal

from backtest_service.strategies.base import Order
from backtest_service.types import TradeSide

if TYPE_CHECKING:
    from quant_engine.trend import TrendModel


_MIN_LOOKBACK = 210  # ema200 + buffer


class AtlasStrategy:
    """Wraps the production pipeline for offline replay.

    Performance note: re-computing all 25 indicators on each bar is O(N²) over
    the backtest. For Phase 1.5 with 5k bars on 1 symbol that's still under a
    second. When we backtest the full S&P 500 we'll cache rolling features —
    flagged in ADR-0003 (to be written).
    """

    name = "atlas"

    def __init__(
        self,
        trend_model: TrendModel | None = None,
        risk: RiskEngine | None = None,
        asset_class: AssetClass = AssetClass.EQUITY,
        horizon: Horizon = Horizon.SWING,
        feature_step: int = 5,
    ):
        self.trend_model = trend_model
        self.risk = risk or RiskEngine()
        self.asset_class = asset_class
        self.horizon = horizon
        # Only re-compute features every `feature_step` bars to keep
        # complexity manageable in tests. Set to 1 for full-fidelity replay.
        self.feature_step = max(1, int(feature_step))
        self._bar_counter = 0

    def on_bar(
        self,
        symbol: str,
        bars: pd.DataFrame,
        open_positions: dict[str, object],
        equity: float,
    ) -> Order | None:
        self._bar_counter += 1
        if len(bars) < _MIN_LOOKBACK:
            return None
        if symbol in open_positions:
            return None
        if self._bar_counter % self.feature_step != 0:
            return None

        feats_df = compute_features(bars)
        latest = feats_df.iloc[-1].to_dict()
        latest = {k: v for k, v in latest.items() if not (isinstance(v, float) and pd.isna(v))}
        latest["close"] = float(bars["close"].iloc[-1])

        p_up: float | None = None
        if self.trend_model is not None:
            p_up = self.trend_model.predict_proba(feats_df)

        outcome = generate_signal(
            symbol=symbol,
            asset_class=self.asset_class,
            horizon=self.horizon,
            features=latest,
            p_up=p_up,
            regime="unknown",
            model_versions={"trend": self.trend_model.version} if self.trend_model else {},
        )
        if isinstance(outcome, GateResult):
            return None
        signal = outcome

        sized = self.risk.build_plan(signal)
        if isinstance(sized, RiskVeto):
            return None

        if sized.entry_price is None or sized.stop_price is None or sized.position_size_pct is None:
            return None

        notional = (sized.position_size_pct / 100.0) * equity
        qty = notional / sized.entry_price
        if qty <= 0:
            return None

        side = TradeSide.LONG if sized.direction == Direction.LONG else TradeSide.SHORT
        return Order(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=float(sized.entry_price),
            stop_price=float(sized.stop_price),
            targets=[float(t) for t in sized.take_profit_levels],
            notes={"composite": sized.composite_score, "confidence": sized.confidence_pct},
        )
