"""Top-level Risk Engine: takes a Signal, returns a sized Signal or vetos.

The veto path is preferred over silently shrinking trades — a 0.01% position
is noise, not risk-managed exposure.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas_shared.schemas import Direction, Signal

from risk_engine.portfolio import PortfolioContext
from risk_engine.sizing import RiskConfig, size_position


@dataclass(frozen=True)
class RiskVeto:
    """Reason a signal was rejected. Logged for calibration & analysis."""

    reason: str
    detail: str = ""


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()

    def build_plan(
        self,
        signal: Signal,
        portfolio: PortfolioContext | None = None,
        realized_vol_annual: float | None = None,
        sector: str | None = None,
        adv_dollars_20d: float | None = None,
    ) -> Signal | RiskVeto:
        """Return a sized Signal, or a RiskVeto explaining the rejection."""
        cfg = self.config

        if signal.direction == Direction.FLAT:
            return RiskVeto("flat_direction")

        if signal.entry_price is None or signal.stop_price is None:
            return RiskVeto("missing_entry_or_stop")

        if signal.confidence_pct / 100.0 < cfg.min_confidence:
            return RiskVeto("low_confidence", f"{signal.confidence_pct:.1f}%")

        if (signal.expected_rr or 0) < cfg.min_rr:
            return RiskVeto("low_rr", f"{signal.expected_rr}")

        # Portfolio-level guardrails.
        max_corr = 0.0
        if portfolio is not None:
            if portfolio.has_existing(signal.symbol):
                return RiskVeto("already_holding", signal.symbol)

            if signal.direction == Direction.LONG and portfolio.current_drawdown_pct > portfolio.max_drawdown_pct:
                return RiskVeto(
                    "drawdown_circuit_breaker",
                    f"{portfolio.current_drawdown_pct:.2%}",
                )

            sector_w = portfolio.sector_weight(sector)
            if sector_w >= portfolio.sector_cap_pct:
                return RiskVeto("sector_cap_breached", f"{sector_w:.2%}")

            max_corr = portfolio.max_corr_to_book(signal.symbol)

        confidence = signal.confidence_pct / 100.0
        size_pct, breakdown = size_position(
            entry=float(signal.entry_price),
            stop=float(signal.stop_price),
            confidence=confidence,
            expected_rr=float(signal.expected_rr or 0),
            config=cfg,
            asset_class=signal.asset_class.value,
            realized_vol_annual=realized_vol_annual,
            max_corr_to_book=max_corr,
            adv_dollars_20d=adv_dollars_20d,
        )

        # Sub-cent positions are administrative noise.
        if size_pct * cfg.account_equity < 1.0:
            return RiskVeto("size_too_small", f"{size_pct:.6f}")

        update: dict = {"position_size_pct": round(size_pct * 100.0, 3)}

        # Carry the breakdown into rationale_md as a small machine-readable
        # tail. The LLM explainer reads this and explains in prose.
        update["rationale_md"] = (
            (signal.rationale_md or "")
            + "\n<!-- sizing_breakdown="
            + repr(breakdown)
            + " -->"
        )
        return signal.model_copy(update=update)


def build_plan(signal: Signal, **kwargs) -> Signal | RiskVeto:
    """Module-level convenience."""
    return RiskEngine().build_plan(signal, **kwargs)
