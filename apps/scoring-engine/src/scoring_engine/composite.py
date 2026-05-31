"""Composite score and default regime-conditional weights.

Phase 1 will:
  - Load weights from `weights[asset_class][horizon][regime]` (yaml).
  - Re-fit weights monthly with walk-forward ridge regression on excess returns.
For Phase 0 we only define defaults + the clamp/blend math.

`composite()` supports an explicit `active` set so deferred or not-yet-wired
sub-scores (e.g. `fund`, `chain`) don't permanently steal weight from the
ones that *are* wired. With no `active` argument the function preserves the
historical blueprint-default behavior (all 8 sub-scores at their published
weights) — that keeps the public API stable for direct callers and tests.
"""

from __future__ import annotations

from collections.abc import Iterable

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

# Every sub-score key we know about — single source of truth for iteration.
ALL_SUBSCORES: tuple[str, ...] = (
    "tech", "quant", "fund", "macro", "sent", "opt", "liq", "chain",
)


def composite(
    subs: SubScores,
    weights: dict[str, float] = DEFAULT_WEIGHTS,
    active: Iterable[str] | None = None,
) -> float:
    """Weighted sum, clamped to [-100, +100].

    If `active` is provided, weights are *renormalised* over the active set so
    the inactive ones contribute nothing AND don't dilute the total. Example
    with the default weights and `active={"tech","quant","liq"}`:

        norm = 0.20 + 0.25 + 0.05 = 0.50
        effective tech weight  = 0.20 / 0.50 = 0.40
        effective quant weight = 0.25 / 0.50 = 0.50
        effective liq   weight = 0.05 / 0.50 = 0.10

    With `active=None` (the historical behavior), every sub-score in `weights`
    contributes at its blueprint-default weight — useful for tests, the
    backtest harness, and any caller that explicitly wants the full
    composition.
    """
    keys = list(active) if active is not None else list(weights)
    norm = sum(weights.get(k, 0.0) for k in keys) or 1.0
    raw = sum((weights.get(k, 0.0) / norm) * getattr(subs, k) for k in keys)
    return max(-100.0, min(100.0, raw))
