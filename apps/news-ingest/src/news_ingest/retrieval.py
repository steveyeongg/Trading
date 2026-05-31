"""Read-side: pull recent scored news for a ticker into sentiment_features.

Used by the signal-service to build the `sentiment_features` dict that
`scoring_engine.s_sent` consumes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atlas_shared.db import session_scope
from sqlalchemy import text

_QUERY = text(
    """
    SELECT ni.title, ni.summary, ni.published_at, ni.salience,
           ns.score, ns.confidence
    FROM news_items ni
    JOIN news_scores ns ON ns.news_id = ni.id
    WHERE :ticker = ANY(ni.tickers)
      AND ni.published_at >= :since
      AND ns.scorer = :scorer
    ORDER BY ni.published_at DESC
    LIMIT :limit
    """
)


async def recent_sentiment_features(
    ticker: str,
    hours: float = 48.0,
    scorer: str = "lexicon",
    limit: int = 100,
    half_life_hours: float = 12.0,
) -> dict[str, float]:
    """Return a flat dict shaped for `s_sent()`:
        {news_sent_score, news_sent_confidence, news_count}

    Aggregates using salience * scorer-confidence * time-decay, same math
    as `sentiment_engine.aggregator.aggregate`. We re-implement it here
    against the SQL rowset to avoid round-tripping `ScoredItem` objects.
    """
    import math

    since = datetime.now(UTC) - timedelta(hours=hours)
    async with session_scope() as s:
        result = await s.execute(
            _QUERY,
            {"ticker": ticker.upper(), "since": since, "scorer": scorer, "limit": limit},
        )
        rows = result.mappings().all()

    if not rows:
        return {"news_sent_score": 0.0, "news_sent_confidence": 0.0, "news_count": 0.0}

    now = datetime.now(UTC)
    decay_per_hour = math.log(2) / half_life_hours
    num = 0.0
    den = 0.0
    conf_sum = 0.0
    for r in rows:
        ts: datetime = r["published_at"]
        age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
        decay = math.exp(-decay_per_hour * age_h)
        w = float(r["confidence"]) * float(r["salience"]) * decay
        if w <= 0:
            continue
        num += float(r["score"]) * w
        den += w
        conf_sum += float(r["confidence"])

    if den == 0:
        return {"news_sent_score": 0.0, "news_sent_confidence": 0.0, "news_count": float(len(rows))}

    return {
        "news_sent_score": float(max(-1.0, min(1.0, num / den))),
        "news_sent_confidence": float(min(1.0, conf_sum / max(1, len(rows)))),
        "news_count": float(len(rows)),
    }
