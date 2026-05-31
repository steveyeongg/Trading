"""Risk engine: sizing math + veto paths."""

from __future__ import annotations

from datetime import UTC, datetime
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
from risk_engine import (
    OpenPosition,
    PortfolioContext,
    RiskConfig,
    RiskEngine,
    RiskVeto,
    cornish_fisher_var,
    historical_var,
    kelly_fraction,
    size_position,
)


def _mk_signal(
    *,
    direction: Direction = Direction.LONG,
    entry: float | None = 100.0,
    stop: float | None = 97.0,
    confidence: float = 70.0,
    rr: float = 2.0,
) -> Signal:
    return Signal(
        id=uuid4(),
        symbol="TEST",
        asset_class=AssetClass.EQUITY,
        generated_at=datetime.now(UTC),
        horizon=Horizon.SWING,
        direction=direction,
        composite_score=70.0,
        confidence_pct=confidence,
        conviction=Conviction.HIGH,
        regime="unknown",
        sub_scores=SubScores(tech=50, quant=50),
        entry_price=entry,
        stop_price=stop,
        take_profit_levels=[105.0, 110.0, 115.0],
        position_size_pct=None,
        expected_rr=rr,
        rationale_md=None,
        model_versions={},
    )


# --- Sizing primitives -----------------------------------------------------


def test_kelly_zero_when_no_edge() -> None:
    assert kelly_fraction(0.5, 1.0) == 0.0


def test_kelly_positive_when_edge() -> None:
    f = kelly_fraction(0.6, 2.0)
    # f* = (0.6*2 - 0.4)/2 = 0.4
    assert f == pytest.approx(0.4, abs=1e-9)


def test_kelly_clipped() -> None:
    # At p=0.99, b=10: f* = (0.99*10 - 0.01)/10 = 0.989. Not yet clipped.
    assert kelly_fraction(0.99, 10.0) == pytest.approx(0.989, abs=1e-9)
    # At absurd p≈1, b≥1, full-Kelly hits >1 and the clip activates.
    assert kelly_fraction(1.0, 1.0) == 1.0


def test_size_position_chooses_smallest_cap() -> None:
    """With a tight stop the ATR method would lever to 100%; the smallest
    binding cap should win. At confidence=0.7, conf-scaled = (0.2/0.5)*0.05 = 0.02."""
    cfg = RiskConfig(account_equity=100_000.0, account_risk_per_trade=0.005)
    size, br = size_position(
        entry=100.0, stop=99.5, confidence=0.7, expected_rr=2.0, config=cfg,
        realized_vol_annual=0.20,
    )
    # ATR-risk is enormous (100% notional) — the conf-scaled cap binds.
    assert br.pct_atr_risk == pytest.approx(1.0, abs=1e-9)
    assert size == pytest.approx(br.pct_confidence_scaled, abs=1e-9)
    assert 0 < size <= cfg.per_asset_cap_equity
    assert br.chosen == size


def test_size_position_vol_target_bites_in_high_vol() -> None:
    cfg = RiskConfig(account_equity=100_000.0, vol_target_annual=0.12)
    size, br = size_position(
        entry=100.0, stop=80.0, confidence=0.7, expected_rr=2.0, config=cfg,
        realized_vol_annual=0.60,  # 60% annualised — very vol-y
    )
    # vol target = 0.12/0.60 = 0.20 but caps + others should pull lower.
    assert size <= cfg.per_asset_cap_equity
    assert br.pct_vol_target == pytest.approx(0.20, abs=1e-9)


def test_size_position_invalid_inputs() -> None:
    cfg = RiskConfig()
    size, _ = size_position(entry=100.0, stop=100.0, confidence=0.7, expected_rr=2.0, config=cfg)
    assert size == 0.0
    size, _ = size_position(entry=0.0, stop=99.0, confidence=0.7, expected_rr=2.0, config=cfg)
    assert size == 0.0


def test_historical_var() -> None:
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.01, size=10_000)
    var95 = historical_var(rets, alpha=0.05)
    # ~1.645 * sigma for a Normal — give the empirical estimate some slack.
    assert 0.012 < var95 < 0.022


def test_cornish_fisher_falls_back_when_short() -> None:
    rets = np.array([0.01, -0.02, 0.005])
    cf = cornish_fisher_var(rets, alpha=0.05)
    h = historical_var(rets, alpha=0.05)
    assert cf == h


# --- Risk engine veto paths -----------------------------------------------


def test_engine_vetos_flat_signal() -> None:
    out = RiskEngine().build_plan(_mk_signal(direction=Direction.FLAT))
    assert isinstance(out, RiskVeto)
    assert out.reason == "flat_direction"


def test_engine_vetos_missing_stop() -> None:
    out = RiskEngine().build_plan(_mk_signal(stop=None))
    assert isinstance(out, RiskVeto)
    assert out.reason == "missing_entry_or_stop"


def test_engine_vetos_low_confidence() -> None:
    out = RiskEngine().build_plan(_mk_signal(confidence=40.0))
    assert isinstance(out, RiskVeto)
    assert out.reason == "low_confidence"


def test_engine_vetos_low_rr() -> None:
    out = RiskEngine().build_plan(_mk_signal(rr=1.0))
    assert isinstance(out, RiskVeto)
    assert out.reason == "low_rr"


def test_engine_sizes_a_clean_signal() -> None:
    out = RiskEngine().build_plan(_mk_signal())
    assert isinstance(out, Signal)
    assert out.position_size_pct is not None
    assert 0 < out.position_size_pct <= 5.0  # ≤ per_asset_cap (5%)


def test_engine_vetos_when_already_holding() -> None:
    ctx = PortfolioContext(
        equity=100_000.0,
        positions=[OpenPosition(symbol="TEST", asset_class="equity", sector="IT", notional=2000, weight_pct=2.0)],
    )
    out = RiskEngine().build_plan(_mk_signal(), portfolio=ctx)
    assert isinstance(out, RiskVeto)
    assert out.reason == "already_holding"


def test_engine_vetos_on_drawdown_circuit_breaker() -> None:
    ctx = PortfolioContext(
        equity=100_000.0,
        max_drawdown_pct=0.10,
        current_drawdown_pct=0.15,
    )
    out = RiskEngine().build_plan(_mk_signal(), portfolio=ctx)
    assert isinstance(out, RiskVeto)
    assert out.reason == "drawdown_circuit_breaker"


def test_engine_vetos_on_sector_cap() -> None:
    ctx = PortfolioContext(
        equity=100_000.0,
        sector_cap_pct=0.10,
        positions=[
            OpenPosition(symbol="AAPL", asset_class="equity", sector="IT", notional=11000, weight_pct=11.0),
        ],
    )
    out = RiskEngine().build_plan(_mk_signal(), portfolio=ctx, sector="IT")
    assert isinstance(out, RiskVeto)
    assert out.reason == "sector_cap_breached"
