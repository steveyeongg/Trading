"""Composite score and BLUEPRINT §8.2 weights.

`composite()` supports an explicit `active` set so sub-scores whose adapter is
not yet wired for a given request don't steal weight from the ones that *are*
wired. Weights are renormalised over the active set.

Sign convention: positive = bullish, negative = bearish. Clamped to [-100, +100].
"""

from __future__ import annotations

from collections.abc import Iterable

from atlas_shared.schemas import SubScores

# BLUEPRINT §8.2 — equities, swing horizon. Tech + quant dominate intentionally;
# news / sentiment / macro enhance conviction, never replace price action.
DEFAULT_WEIGHTS: dict[str, float] = {
    "tech": 0.30,
    "quant": 0.25,
    "news": 0.10,
    "sent": 0.10,
    "macro": 0.10,
    "opt": 0.05,
    "liq": 0.05,
    "risk": 0.05,
}

ALL_SUBSCORES: tuple[str, ...] = tuple(DEFAULT_WEIGHTS.keys())


def composite(
    subs: SubScores,
    weights: dict[str, float] = DEFAULT_WEIGHTS,
    active: Iterable[str] | None = None,
) -> float:
    """Weighted sum, clamped to [-100, +100].

    If `active` is provided, weights are renormalised over the active set so
    inactive engines contribute zero AND don't dilute the total. With
    `active=None`, every key in `weights` contributes at its published weight.
    """
    keys = list(active) if active is not None else list(weights)
    norm = sum(weights.get(k, 0.0) for k in keys) or 1.0
    raw = sum((weights.get(k, 0.0) / norm) * getattr(subs, k) for k in keys)
    return max(-100.0, min(100.0, raw))
