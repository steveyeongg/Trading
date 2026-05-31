"""Options analytics: Greeks, chain analytics, and the s_opt sub-score."""

from __future__ import annotations

import math

from options_analytics import (
    bs_gamma,
    bs_price,
    compute_features,
    implied_vol,
    s_options,
    synthetic_chain,
)

# --- Black-Scholes ---------------------------------------------------------


def test_put_call_parity() -> None:
    s, k, t, r, sig = 100.0, 100.0, 0.5, 0.04, 0.25
    c = bs_price(s, k, t, r, sig, "call")
    p = bs_price(s, k, t, r, sig, "put")
    # c - p = s - k*e^{-rt}
    assert math.isclose(c - p, s - k * math.exp(-r * t), abs_tol=1e-6)


def test_gamma_positive_atm() -> None:
    assert bs_gamma(100.0, 100.0, 0.5, 0.04, 0.25) > 0


def test_implied_vol_recovers_input() -> None:
    s, k, t, r, sig = 100.0, 105.0, 0.4, 0.04, 0.33
    price = bs_price(s, k, t, r, sig, "call")
    iv = implied_vol(price, s, k, t, r, "call")
    assert math.isclose(iv, sig, abs_tol=1e-3)


def test_call_price_monotonic_in_vol() -> None:
    lo = bs_price(100, 100, 0.5, 0.04, 0.1, "call")
    hi = bs_price(100, 100, 0.5, 0.04, 0.5, "call")
    assert hi > lo


# --- chain analytics -------------------------------------------------------


def test_synthetic_chain_deterministic() -> None:
    a = synthetic_chain("AAPL", 200.0, seed=1)
    b = synthetic_chain("AAPL", 200.0, seed=1)
    assert [q.last for q in a.quotes] == [q.last for q in b.quotes]


def test_put_skew_lifts_put_oi_ratio() -> None:
    balanced = compute_features(synthetic_chain("X", 100.0, pc_oi_ratio=1.0))
    heavy_puts = compute_features(synthetic_chain("X", 100.0, pc_oi_ratio=2.0))
    assert heavy_puts.put_call_oi > balanced.put_call_oi


def test_max_pain_near_spot_for_symmetric_chain() -> None:
    feats = compute_features(synthetic_chain("X", 100.0, pc_oi_ratio=1.0, seed=3))
    # Max pain should land within the strike grid (±25%).
    assert 75.0 <= feats.max_pain <= 125.0


def test_iv_rank_in_unit_interval() -> None:
    feats = compute_features(synthetic_chain("X", 100.0))
    assert 0.0 <= feats.iv_rank <= 1.0


def test_gex_is_finite() -> None:
    feats = compute_features(synthetic_chain("X", 100.0))
    assert math.isfinite(feats.gex)


# --- s_opt -----------------------------------------------------------------


def test_s_opt_neutral_on_empty() -> None:
    assert s_options({}) == 0.0


def test_s_opt_bearish_on_heavy_puts() -> None:
    assert s_options({"put_call_oi": 2.5}) < 0


def test_s_opt_bullish_on_call_flow() -> None:
    assert s_options({"put_call_oi": 0.5, "uoa_z": 2.0}) > 0


def test_s_opt_clamped() -> None:
    out = s_options({"put_call_oi": 0.01, "uoa_z": 10, "gex": 1e12})
    assert -100.0 <= out <= 100.0
