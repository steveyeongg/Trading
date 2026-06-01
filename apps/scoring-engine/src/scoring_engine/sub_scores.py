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


def _block_trend(features: dict[str, Any]) -> float:
    """25% block — EMA stack alignment, ADX trend strength, Supertrend dir."""
    score = 0.0

    e9, e21, e50, e200 = (
        _get(features, "ema9"),
        _get(features, "ema21"),
        _get(features, "ema50"),
        _get(features, "ema200"),
    )
    if e9 and e21 and e50 and e200:
        if e9 > e21 > e50 > e200:
            score += 50.0
        elif e9 < e21 < e50 < e200:
            score -= 50.0
        elif e9 > e21 > e50:
            score += 25.0
        elif e9 < e21 < e50:
            score -= 25.0

    # ADX strength — small positive lift; the sign comes from DI+ vs DI-.
    adx = _get(features, "adx", 0.0)
    dip = _get(features, "di_plus", 0.0)
    dim = _get(features, "di_minus", 0.0)
    adx_strength = min(adx, 50.0) / 50.0  # 0..1
    if dip > dim:
        score += 25.0 * adx_strength
    elif dim > dip:
        score -= 25.0 * adx_strength

    # Supertrend direction acts as a confirming flip.
    st_dir = _get(features, "supertrend_dir", 0.0)
    if st_dir > 0:
        score += 25.0
    elif st_dir < 0:
        score -= 25.0

    return float(np.clip(score, -100.0, 100.0))


def _block_momentum(features: dict[str, Any]) -> float:
    """20% block — RSI, MACD histogram, Stochastic."""
    score = 0.0

    rsi = _get(features, "rsi14", 50.0)
    score += _sigmoid_curve(rsi, mid=50.0, slope=0.06) * 45.0

    macd_hist = _get(features, "macd_hist", 0.0)
    atr = _get(features, "atr14", 0.0)
    if atr > 0:
        score += float(np.tanh(macd_hist / atr)) * 35.0

    # Stochastic — confirming, smaller weight.
    stoch_k = _get(features, "stoch_k", 50.0)
    score += _sigmoid_curve(stoch_k, mid=50.0, slope=0.05) * 20.0

    return float(np.clip(score, -100.0, 100.0))


def _block_volatility(features: dict[str, Any]) -> float:
    """15% block — Bollinger %b position + Keltner expansion."""
    score = 0.0

    bb_pctb = _get(features, "bb_pctb", 0.5)
    # %b >> 0.5 = overextended high → mean-revert pressure (negative for
    # mean-reversion bias, positive for trend continuation). For a §5.3
    # composite read, treat the *direction* of the deviation as mildly
    # mean-reverting — extremes shouldn't drive a buy.
    score += -float(np.tanh((bb_pctb - 0.5) * 4.0)) * 60.0

    # Close vs Keltner middle — sign reinforces the trend block.
    close = _get(features, "close", 0.0)
    kel_mid = _get(features, "keltner_middle", close)
    if kel_mid and close:
        delta = (close - kel_mid) / kel_mid
        score += float(np.tanh(delta * 20)) * 40.0

    return float(np.clip(score, -100.0, 100.0))


def _block_volume(features: dict[str, Any]) -> float:
    """15% block — OBV slope z-score + relative volume confirmation."""
    obv_z = _get(features, "obv_slope_z", 0.0)
    score = float(np.tanh(obv_z)) * 70.0

    rvol = _get(features, "rvol_20", 1.0)
    # rvol > 1.5 amplifies the existing direction; thin tape (< 0.7) dampens.
    if rvol >= 1.5:
        score *= 1.25
    elif rvol < 0.7:
        score *= 0.75

    # Volume above average without OBV direction → mildly positive bias.
    if rvol >= 1.5 and abs(obv_z) < 0.1:
        score += 15.0

    return float(np.clip(score, -100.0, 100.0))


def _block_structure(features: dict[str, Any]) -> float:
    """15% block — SMC BOS, divergences, Donchian breakout, Fibonacci position."""
    score = 0.0

    bos = _get(features, "smc_bos", 0.0)
    if bos > 0.5:
        score += 35.0
    elif bos < -0.5:
        score -= 35.0

    if _get(features, "divergence_bull", 0.0) > 0.5:
        score += 20.0
    if _get(features, "divergence_bear", 0.0) > 0.5:
        score -= 20.0

    # Donchian breakout — close near the channel edge.
    close = _get(features, "close", 0.0)
    dc_up = _get(features, "donchian_upper", 0.0)
    dc_lo = _get(features, "donchian_lower", 0.0)
    if dc_up and dc_lo and dc_up > dc_lo and close:
        rng = dc_up - dc_lo
        # Position within channel ∈ [-1, 1] (clamp).
        pos = max(-1.0, min(1.0, 2 * (close - dc_lo) / rng - 1.0))
        score += pos * 25.0

    # Fibonacci position — extremes lose conviction (mean-revert risk).
    fib = _get(features, "fib_position", 0.5)
    if fib >= 0.95:
        score -= 10.0  # overextended at swing high
    elif fib <= 0.05:
        score += 10.0  # near swing low → bullish reversion bias

    return float(np.clip(score, -100.0, 100.0))


def _block_mtf(features: dict[str, Any]) -> float:
    """10% block — multi-timeframe confirmation flag.

    Set by the caller (the pipeline that has higher-timeframe context).
    Absent → 0 (neutral, no boost / no penalty)."""
    mtf = _get(features, "mtf_confirm", 0.0)
    return float(np.clip(mtf * 100.0, -100.0, 100.0))


# BLUEPRINT §5.3 — explicit weighted blocks. Each block returns [-100, +100];
# the s_tech score is the weighted sum, then clamped to [-100, +100].
_S_TECH_WEIGHTS: dict[str, float] = {
    "trend": 0.25,
    "momentum": 0.20,
    "volatility": 0.15,
    "volume": 0.15,
    "structure": 0.15,
    "mtf": 0.10,
}


def s_tech(features: dict[str, Any]) -> float:
    """Technical sub-score. BLUEPRINT §5.3 weighted-block formula.

    25% trend + 20% momentum + 15% volatility + 15% volume + 15% structure
    + 10% multi-timeframe confirmation.

    Each block is computed independently from a NaN-safe feature dict; missing
    indicators collapse to neutral so a partial feature set still produces a
    sensible score rather than crashing.
    """
    blocks = {
        "trend": _block_trend(features),
        "momentum": _block_momentum(features),
        "volatility": _block_volatility(features),
        "volume": _block_volume(features),
        "structure": _block_structure(features),
        "mtf": _block_mtf(features),
    }
    score = sum(_S_TECH_WEIGHTS[k] * v for k, v in blocks.items())
    return float(np.clip(score, -100.0, 100.0))


def s_tech_breakdown(features: dict[str, Any]) -> dict[str, float]:
    """Expose the per-block s_tech contributions for the /debug endpoint."""
    return {
        "trend": _block_trend(features),
        "momentum": _block_momentum(features),
        "volatility": _block_volatility(features),
        "volume": _block_volume(features),
        "structure": _block_structure(features),
        "mtf": _block_mtf(features),
    }


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


def s_news(news_features: dict[str, Any] | None) -> float:
    """News-flow sub-score in [-100, +100]. BLUEPRINT §7.1 / §8.1.

    Combines tone (news_sent_score in [-1, 1]) with saturating count and the
    scorer's own confidence. Zero count or missing dict → 0 (neutral), never
    a spurious bias.

    Expected keys (all optional):
      news_sent_score      : float in [-1, 1] — net tone (recency/salience weighted upstream)
      news_sent_confidence : float in [0, 1]  — scorer confidence aggregate
      news_count           : float            — headline count over the lookback window
    """
    if not news_features:
        return 0.0
    tone = _get(news_features, "news_sent_score", 0.0)
    conf = _get(news_features, "news_sent_confidence", 0.0)
    count = _get(news_features, "news_count", 0.0)
    if count <= 0:
        return 0.0
    # Saturate at ~10 headlines — beyond that more news doesn't increase magnitude.
    coverage = 1.0 - float(np.exp(-count / 5.0))
    magnitude = max(0.2, conf)  # never zero out a strong signal because confidence is low
    return float(np.clip(tone * coverage * magnitude * 100.0, -100.0, 100.0))


def s_risk(features: dict[str, Any]) -> float:
    """Trade-acceptability sub-score in [-100, +100]. BLUEPRINT §8.1.

    Pre-screen for the risk engine — answers "does this look tradeable at all"
    based on per-bar features the scoring stage already has. The risk engine
    does the full sizing/veto pass later.

    Heuristics:
      - ATR/price in the 0.5%–4% band (healthy realised vol) → positive.
      - ATR/price too tight (<0.5%) → suggests illiquid / manipulable → negative.
      - ATR/price too wild (>8%) → uninvestable risk → negative.
      - Realised vol z-score: extreme readings clip the score back to neutral.
    """
    atr = _get(features, "atr14", 0.0)
    close = _get(features, "close", 0.0)
    if atr <= 0 or close <= 0:
        return 0.0
    atr_pct = atr / close

    if atr_pct < 0.003:
        base = -60.0  # suspiciously tight; signal is fragile
    elif atr_pct < 0.005:
        base = -20.0
    elif atr_pct <= 0.04:
        base = 50.0   # sweet spot
    elif atr_pct <= 0.08:
        base = 0.0    # tradeable but heavy
    else:
        base = -60.0  # too volatile to size sensibly

    vol_z = _get(features, "vol_realized_20", 0.0)
    if abs(vol_z) > 3.0:
        base = min(base, 0.0)
    return float(np.clip(base, -100.0, 100.0))
