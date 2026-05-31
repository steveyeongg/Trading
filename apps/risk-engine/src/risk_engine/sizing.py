"""Position sizing primitives.

The Risk Engine combines several caps and uses the *minimum* — never larger
than any one of them. BLUEPRINT §10.1.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class RiskConfig:
    """Per-account risk policy. Persisted per user/portfolio in production."""

    account_equity: float = 100_000.0
    # Fraction of equity risked on any single trade if stop is hit. 0.5% default.
    account_risk_per_trade: float = 0.005
    # Annualised portfolio vol target. Used to derive per-position weight.
    vol_target_annual: float = 0.12
    # Fraction of full Kelly to deploy. Half- or quarter-Kelly is standard for
    # quants since estimates of edge and odds are noisy.
    kelly_cap: float = 0.25
    # Hard cap on any single position as fraction of equity.
    per_asset_cap_equity: float = 0.05
    per_asset_cap_crypto: float = 0.03
    # Trade must clear this expected R:R or we veto.
    min_rr: float = 1.5
    # Confidence below this floor → veto regardless of composite score.
    min_confidence: float = 0.55
    # Liquidity cap: position ≤ this fraction of 20-day ADV.
    adv_participation_cap: float = 0.01

    def per_asset_cap(self, asset_class: str) -> float:
        if asset_class == "crypto":
            return self.per_asset_cap_crypto
        return self.per_asset_cap_equity


def kelly_fraction(p_win: float, win_loss_ratio: float) -> float:
    """Classic Kelly: f* = (p * b - q) / b.

    p = P(win), b = avg_win / avg_loss (the payoff ratio), q = 1 - p.
    Clipped to [0, 1].
    """
    if win_loss_ratio <= 0:
        return 0.0
    f = (p_win * win_loss_ratio - (1.0 - p_win)) / win_loss_ratio
    return float(np.clip(f, 0.0, 1.0))


@dataclass(frozen=True)
class SizingBreakdown:
    """Diagnostics for why a position ended up the size it did."""

    pct_atr_risk: float
    pct_kelly: float
    pct_vol_target: float
    pct_confidence_scaled: float
    pct_correlation: float
    pct_adv_cap: float
    pct_asset_cap: float
    chosen: float


def size_position(
    *,
    entry: float,
    stop: float,
    confidence: float,
    expected_rr: float,
    config: RiskConfig,
    asset_class: str = "equity",
    realized_vol_annual: float | None = None,
    max_corr_to_book: float = 0.0,
    adv_dollars_20d: float | None = None,
) -> tuple[float, SizingBreakdown]:
    """Return (position_size_pct_of_equity, breakdown).

    Size 0 = veto. The smallest cap wins.
    """
    if entry <= 0 or stop <= 0 or entry == stop:
        return 0.0, SizingBreakdown(0, 0, 0, 0, 0, 0, 0, 0)

    risk_per_unit = abs(entry - stop)

    # 1. Account-risk method: total $ at risk = equity * risk_per_trade.
    dollar_risk = config.account_equity * config.account_risk_per_trade
    units = dollar_risk / risk_per_unit
    notional = units * entry
    pct_atr = notional / config.account_equity

    # 2. Kelly.
    pct_kelly = kelly_fraction(confidence, max(expected_rr, 0.1)) * config.kelly_cap

    # 3. Vol target.
    if realized_vol_annual and realized_vol_annual > 0:
        pct_vol = config.vol_target_annual / realized_vol_annual
    else:
        # No vol estimate → fall back to the asset cap as a ceiling.
        pct_vol = config.per_asset_cap(asset_class)

    # 4. Confidence-scaled base. Confidence ≤ 0.5 → 0 contribution.
    pct_conf = max(0.0, (confidence - 0.5) / 0.5) * config.per_asset_cap(asset_class)

    # 5. Correlation: reduce as the existing book already covers this risk.
    pct_corr = config.per_asset_cap(asset_class) * (1.0 - float(np.clip(max_corr_to_book, 0.0, 1.0)))

    # 6. Liquidity (ADV participation).
    if adv_dollars_20d and adv_dollars_20d > 0:
        pct_adv = (config.adv_participation_cap * adv_dollars_20d) / config.account_equity
    else:
        pct_adv = config.per_asset_cap(asset_class)

    # 7. Hard asset cap.
    pct_cap = config.per_asset_cap(asset_class)

    chosen = float(min(pct_atr, pct_kelly, pct_vol, pct_conf, pct_corr, pct_adv, pct_cap))
    chosen = max(0.0, chosen)
    return chosen, SizingBreakdown(
        pct_atr_risk=pct_atr,
        pct_kelly=pct_kelly,
        pct_vol_target=pct_vol,
        pct_confidence_scaled=pct_conf,
        pct_correlation=pct_corr,
        pct_adv_cap=pct_adv,
        pct_asset_cap=pct_cap,
        chosen=chosen,
    )


def historical_var(returns: np.ndarray, alpha: float = 0.05) -> float:
    """Historical Value-at-Risk. Returns the (positive) loss at the alpha quantile."""
    if returns.size == 0:
        return 0.0
    return float(-np.quantile(returns, alpha))


def cornish_fisher_var(returns: np.ndarray, alpha: float = 0.05) -> float:
    """Cornish-Fisher VaR — adjusts for skew and kurtosis. BLUEPRINT §10.3."""
    if returns.size < 30:
        return historical_var(returns, alpha)
    from scipy import stats

    mu = float(np.mean(returns))
    sig = float(np.std(returns, ddof=1))
    s = float(stats.skew(returns, bias=False))
    k = float(stats.kurtosis(returns, fisher=True, bias=False))
    z = float(stats.norm.ppf(alpha))
    z_cf = z + (z**2 - 1) * s / 6 + (z**3 - 3 * z) * k / 24 - (2 * z**3 - 5 * z) * (s**2) / 36
    return float(-(mu + sig * z_cf))
