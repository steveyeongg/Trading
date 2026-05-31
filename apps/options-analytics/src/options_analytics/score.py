"""s_opt — options sub-score in [-100, +100]. BLUEPRINT §8.1.

Inputs (flat dict, all optional → neutral):
  put_call_oi          : put/call OI ratio (>1 bearish positioning)
  iv_rank              : 0..1 (high = expensive options / fear)
  gex                  : net dealer gamma ($/1% move; +suppresses vol)
  max_pain_distance    : (spot - max_pain)/spot (+ = spot above pain → pull down)
  uoa_z                : unusual-options-activity z-score (bullish call flow)
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _get(d: dict[str, Any], k: str, default: float = 0.0) -> float:
    v = d.get(k)
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if np.isfinite(f) else default


def s_options(opt: dict[str, Any]) -> float:
    score = 0.0

    # Put/call OI: 1.0 is neutral; >1 bearish, <1 bullish. Center the log-ratio.
    pc = _get(opt, "put_call_oi", 1.0)
    if pc > 0:
        score += -float(np.tanh(np.log(pc))) * 25.0   # ±25

    # Unusual call flow (UOA): bullish when positive.
    uoa_z = _get(opt, "uoa_z", 0.0)
    score += float(np.tanh(uoa_z)) * 20.0             # ±20

    # GEX: positive dealer gamma supports a calm, mean-reverting tape — mildly
    # constructive for holding longs; negative GEX = trend/vol amplification.
    gex = _get(opt, "gex", 0.0)
    score += float(np.tanh(gex / 1e9)) * 15.0          # ±15 (scaled to $bn)

    # IV rank: very high IV (>0.8) is a fear/expensive-hedge tell → fade a bit;
    # very low IV (<0.2) is complacency but supportive of trend continuation.
    ivr = _get(opt, "iv_rank", 0.5)
    score += -(ivr - 0.5) * 2.0 * 10.0                 # ±10

    # Max-pain pull: if spot is well above max pain, dealer hedging tends to
    # pull price down into expiry (bearish), and vice versa.
    mpd = _get(opt, "max_pain_distance", 0.0)
    score += -float(np.tanh(mpd * 5.0)) * 10.0         # ±10

    return float(np.clip(score, -100.0, 100.0))
