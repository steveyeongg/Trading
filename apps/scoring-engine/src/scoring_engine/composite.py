"""Composite score and default regime-conditional weights.

Phase 1 will:
  - Load weights from `weights[asset_class][horizon][regime]` (yaml).
  - Re-fit weights monthly with walk-forward ridge regression on excess returns.
For Phase 0 we only define defaults + the clamp/blend math.
"""

from __future__ import annotations

from atlas_shared.schemas import SubScores

# Defaults from BLUEPRINT §8.2 (equities, swing horizon).
DEFAULT_WEIGHTS: dict[str, float] = {
    "tech": 0.20,
    "quant": 0.25,
    "fund": 0.15,
    "macro": 0.10,
    "sent": 0.10,
    "opt": 0.10,
    "liq": 0.05,
    "chain": 0.05,
}


def composite(subs: SubScores, weights: dict[str, float] = DEFAULT_WEIGHTS) -> float:
    """Weighted sum, clamped to [-100, +100]."""
    raw = (
        weights["tech"] * subs.tech
        + weights["quant"] * subs.quant
        + weights["fund"] * subs.fund
        + weights["macro"] * subs.macro
        + weights["sent"] * subs.sent
        + weights["opt"] * subs.opt
        + weights["liq"] * subs.liq
        + weights["chain"] * subs.chain
    )
    return max(-100.0, min(100.0, raw))
