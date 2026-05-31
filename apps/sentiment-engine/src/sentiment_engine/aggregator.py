"""Per-ticker sentiment aggregation.

Combines:
  - per-item salience (how much an item is *actually* about the ticker)
  - per-item confidence (the scorer's certainty)
  - exponential time decay (newer items count more)
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from sentiment_engine.types import ScoredItem


def aggregate(
    scored: list[ScoredItem],
    ticker: str,
    *,
    now: datetime | None = None,
    half_life_hours: float = 12.0,
    min_items: int = 1,
) -> dict[str, float]:
    """Return aggregate {sent_score, sent_count, sent_confidence} for `ticker`.

    `sent_score` is the salience- and confidence- and decay-weighted mean of
    item scores. Returns a zeroed dict if too few items.
    """
    now = now or datetime.now(UTC)
    decay_per_hour = math.log(2) / half_life_hours

    num = 0.0
    den = 0.0
    conf_sum = 0.0
    n = 0
    for s in scored:
        if ticker not in s.item.tickers:
            continue
        # Item age in hours (clamped to non-negative).
        delta_h = max(0.0, (now - s.item.ts).total_seconds() / 3600.0)
        decay = math.exp(-decay_per_hour * delta_h)
        w = s.confidence * s.item.salience * decay
        if w <= 0:
            continue
        num += s.score * w
        den += w
        conf_sum += s.confidence
        n += 1

    if n < min_items or den == 0.0:
        return {"sent_score": 0.0, "sent_count": float(n), "sent_confidence": 0.0}
    return {
        "sent_score": float(max(-1.0, min(1.0, num / den))),
        "sent_count": float(n),
        "sent_confidence": float(min(1.0, conf_sum / max(1, n))),
    }


def trailing_window(items: list[ScoredItem], hours: float = 24.0, now: datetime | None = None) -> list[ScoredItem]:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    return [s for s in items if s.item.ts >= cutoff]
