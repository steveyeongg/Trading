"""Sub-score functions. Each maps a feature vector / context dict to a score
in [-100, +100]. Negative = bearish, positive = bullish.

The technical sub-score is a faithful implementation of BLUEPRINT §8.4.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _sigmoid_curve(x: float, mid: float, slope: float) -> float:
    """Smooth sigmoid centred at `mid`. Returns ~0 at mid, → ±1 at extremes."""
    return float(np.tanh((x - mid) * slope))


def _get(features: dict[str, Any], key: str, default: float = 0.0) -> float:
    """Safe accessor: NaNs and Nones collapse to `default`."""
    v = features.get(key)
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(f):
        return default
    return f


def s_tech(features: dict[str, Any]) -> float:
    """Technical sub-score. BLUEPRINT §8.4.

    Inputs (all read from the feature dict, NaN-safe):
      rsi14, macd_hist, atr14, ema9, ema21, ema50, ema200,
      bb_pctb, adx, obv_slope_z, smc_bos, divergence_bull, divergence_bear,
      mtf_confirm (optional; default 0 → neutral)
    """
    score = 0.0

    rsi = _get(features, "rsi14", 50.0)
    score += _sigmoid_curve(rsi, mid=50.0, slope=0.06) * 20.0  # ±20 cap

    macd_hist = _get(features, "macd_hist", 0.0)
    atr = _get(features, "atr14", 0.0)
    if atr > 0:
        score += float(np.tanh(macd_hist / atr)) * 15.0  # ±15

    e9, e21, e50, e200 = (
        _get(features, "ema9"),
        _get(features, "ema21"),
        _get(features, "ema50"),
        _get(features, "ema200"),
    )
    if e9 and e21 and e50 and e200:
        if e9 > e21 > e50 > e200:
            score += 15.0
        elif e9 < e21 < e50 < e200:
            score -= 15.0

    bb_pctb = _get(features, "bb_pctb", 0.5)
    # When pctb >> 0.5 the asset is overextended high → mean-revert pressure (negative).
    score += -float(np.tanh((bb_pctb - 0.5) * 4.0)) * 10.0  # ±10

    # Trend-strength gate scales everything so far by ADX (clipped to 50).
    adx = _get(features, "adx", 0.0)
    adx_mult = 0.5 + min(adx, 50.0) / 50.0  # 0.5..1.5
    score *= adx_mult

    obv_z = _get(features, "obv_slope_z", 0.0)
    score += float(np.tanh(obv_z)) * 10.0  # ±10

    bos = _get(features, "smc_bos", 0.0)
    if bos > 0.5:
        score += 10.0
    elif bos < -0.5:
        score -= 10.0

    if _get(features, "divergence_bull", 0.0) > 0.5:
        score += 8.0
    if _get(features, "divergence_bear", 0.0) > 0.5:
        score -= 8.0

    mtf = _get(features, "mtf_confirm", 0.0)
    if mtf > 0:
        score *= 1.15
    elif mtf < 0:
        score *= 0.85

    return float(np.clip(score, -100.0, 100.0))


def s_quant(p_up: float | None) -> float:
    """Map calibrated P(up) → [-100, +100]."""
    if p_up is None or not np.isfinite(p_up):
        return 0.0
    return float(np.clip((2.0 * p_up - 1.0) * 100.0, -100.0, 100.0))


def s_liq(features: dict[str, Any]) -> float:
    """Liquidity sub-score. Crude: rewards above-median volume, penalises
    thin tape. Real Phase 1 needs spread + book depth — punt until live feed."""
    vol_z = _get(features, "obv_slope_z", 0.0)  # proxy for volume momentum
    return float(np.clip(vol_z * 10.0, -100.0, 100.0))
