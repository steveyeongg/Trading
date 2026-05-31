"""s_macro — macro sub-score in [-100, +100].

BLUEPRINT §8.1: regime, yield curve, DXY, sector rotation, VIX.
Sign convention: positive = constructive for risk assets (long bias).
"""

from __future__ import annotations

from typing import Any

import numpy as np

_REGIME_BIAS = {
    "risk-on": +35.0,
    "risk-off": -35.0,
    "late-cycle": -10.0,
    "stagflation": -25.0,
    "unknown": 0.0,
}


def _get(d: dict[str, Any], k: str, default: float = 0.0) -> float:
    v = d.get(k)
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(f):
        return default
    return f


def s_macro(macro_features: dict[str, Any]) -> float:
    """Compose the macro sub-score from a flat feature dict.

    Expected keys (all optional, missing → neutral):
      regime              : str — one of REGIMES
      regime_confidence   : float in [0, 1]
      term_spread         : float (10Y-2Y, %)
      vix                 : float — VIX level
      vix_z               : float — 60d z-score of VIX
      dxy_z               : float — 60d z-score of USD index
    """
    score = 0.0

    regime = str(macro_features.get("regime", "unknown"))
    conf = _get(macro_features, "regime_confidence", 0.0)
    score += _REGIME_BIAS.get(regime, 0.0) * max(0.25, conf)

    # Term-spread: deeply inverted → risk-off bias.
    ts = _get(macro_features, "term_spread", 0.0)
    score += float(np.tanh(ts / 0.5)) * 15.0

    # VIX: high → bearish, low → bullish. Use z-score so the function is regime-agnostic.
    vix_z = _get(macro_features, "vix_z", 0.0)
    score += -float(np.tanh(vix_z)) * 20.0

    # DXY: strong dollar typically risk-off for global equities.
    dxy_z = _get(macro_features, "dxy_z", 0.0)
    score += -float(np.tanh(dxy_z)) * 10.0

    # Absolute VIX brake — even with low z-score, VIX > 30 caps the score.
    vix = _get(macro_features, "vix", 0.0)
    if vix > 30:
        score = min(score, 0.0)

    return float(np.clip(score, -100.0, 100.0))
