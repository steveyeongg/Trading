"""Persistence: news_items + news_scores.

Idempotent upserts keyed on (source, external_id). Score writes are also
idempotent so re-scoring with a new model version doesn't duplicate rows.
"""

from __future__ import annotations

from atlas_shared.db import session_scope
from atlas_shared.logging import get_logger
from sentiment_engine.types import ScoredItem
from sqlalchemy import text

from news_ingest.sources.base import RawItem

log = get_logger("news.store")


_UPSERT_ITEM = text(
    """
    INSERT INTO news_items
      (source, external_id, url, title, summary, body, published_at, tickers, salience, metadata)
    VALUES
      (:source, :external_id, :url, :title, :summary, :body, :published_at, :tickers, :salience, CAST(:metadata AS JSONB))
    ON CONFLICT (source, external_id) DO UPDATE SET
      url = EXCLUDED.url,
      title = EXCLUDED.title,
      summary = EXCLUDED.summary,
      body = EXCLUDED.body,
      tickers = EXCLUDED.tickers,
      salience = EXCLUDED.salience,
      metadata = EXCLUDED.metadata
    RETURNING id
    """
)

_UPSERT_SCORE = text(
    """
    INSERT INTO news_scores (news_id, scorer, score, confidence)
    VALUES (:news_id, :scorer, :score, :confidence)
    ON CONFLICT (news_id, scorer) DO UPDATE SET
      score = EXCLUDED.score,
      confidence = EXCLUDED.confidence,
      scored_at = now()
    """
)


async def persist(items: list[RawItem], scored: list[ScoredItem], scorer_name: str) -> int:
    """Persist items and their scores. Returns number of items written."""
    if not items:
        return 0

    # The scorer kept item-order so we can zip without a key.
    if len(scored) != len(items):
        raise ValueError(f"scored ({len(scored)}) and items ({len(items)}) length mismatch")

    import json as _json

    rows = [
        {
            "source": it.source,
            "external_id": it.external_id,
            "url": it.url,
            "title": it.title,
            "summary": it.summary,
            "body": it.body,
            "published_at": it.published_at,
            "tickers": list(it.tickers),
            "salience": 1.0,
            "metadata": _json.dumps(it.metadata or {}),
        }
        for it in items
    ]

    n = 0
    async with session_scope() as s:
        for sc, row in zip(scored, rows, strict=True):
            result = await s.execute(_UPSERT_ITEM, row)
            news_id = int(result.scalar_one())
            await s.execute(
                _UPSERT_SCORE,
                {"news_id": news_id, "scorer": scorer_name, "score": sc.score, "confidence": sc.confidence},
            )
            n += 1
    log.info("news.store.persisted", n=n, scorer=scorer_name)
    return n
