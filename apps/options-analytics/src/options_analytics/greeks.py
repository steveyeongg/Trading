"""Black-Scholes pricing + Greeks. Pure, vectorizable, dependency-light.

Used to compute per-contract gamma for GEX and to back out IV when a vendor
chain doesn't provide it. European, continuous-div approximation — fine for
the index/equity options we score.
"""

from __future__ import annotations

import math
from typing import Literal

from scipy.stats import norm

Right = Literal["call", "put"]


def _d1_d2(s: float, k: float, t: float, r: float, sigma: float) -> tuple[float, float]:
    if t <= 0 or sigma <= 0 or s <= 0 or k <= 0:
        return float("nan"), float("nan")
    d1 = (math.log(s / k) + (r + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return d1, d2


def bs_price(s: float, k: float, t: float, r: float, sigma: float, right: Right) -> float:
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    if math.isnan(d1):
        return max(0.0, (s - k) if right == "call" else (k - s))
    if right == "call":
        return s * norm.cdf(d1) - k * math.exp(-r * t) * norm.cdf(d2)
    return k * math.exp(-r * t) * norm.cdf(-d2) - s * norm.cdf(-d1)


def bs_gamma(s: float, k: float, t: float, r: float, sigma: float) -> float:
    """Gamma is the same for calls and puts."""
    d1, _ = _d1_d2(s, k, t, r, sigma)
    if math.isnan(d1):
        return 0.0
    return norm.pdf(d1) / (s * sigma * math.sqrt(t))


def bs_delta(s: float, k: float, t: float, r: float, sigma: float, right: Right) -> float:
    d1, _ = _d1_d2(s, k, t, r, sigma)
    if math.isnan(d1):
        return 0.0
    return norm.cdf(d1) if right == "call" else norm.cdf(d1) - 1.0


def implied_vol(
    price: float, s: float, k: float, t: float, r: float, right: Right,
    lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-5, max_iter: int = 100,
) -> float:
    """Bisection IV solve. Returns NaN if the price is outside no-arb bounds."""
    if price <= 0 or t <= 0 or s <= 0 or k <= 0:
        return float("nan")
    intrinsic = max(0.0, (s - k) if right == "call" else (k - s))
    if price < intrinsic - 1e-6:
        return float("nan")
    f_lo = bs_price(s, k, t, r, lo, right) - price
    f_hi = bs_price(s, k, t, r, hi, right) - price
    if f_lo * f_hi > 0:
        return float("nan")
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(s, k, t, r, mid, right) - price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)
