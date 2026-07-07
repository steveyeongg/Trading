"""CLI orchestrator: fetch → extract tickers → score → persist.

Examples:
    uv run python -m news_ingest.refresh --source rss
    uv run python -m news_ingest.refresh --source newsapi --query "AAPL OR MSFT"
    uv run python -m news_ingest.refresh --source file --path data/news_seed.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from atlas_shared.logging import get_logger, setup_logging
from sentiment_engine import LexiconScorer
from sentiment_engine.types import NewsItem

from news_ingest.sources import FileSource, NewsApiSource, RssSource
from news_ingest.sources.base import RawItem
from news_ingest.store import persist
from news_ingest.tickers import extract_tickers, salience

setup_logging()
log = get_logger("news.refresh")


def _enrich(items: list[RawItem], universe: set[str] | None) -> list[RawItem]:
    """Apply ticker extraction. Salience is computed per-(item, ticker) and
    stored under each item's metadata as a dict — the store flattens to a
    single float for now; per-ticker salience lands in Phase 2.5."""
    out: list[RawItem] = []
    for it in items:
        text = " ".join(filter(None, [it.title, it.summary, it.body]))
        tickers = tuple(extract_tickers(text, universe=universe))
        if not tickers:
            continue
        per_ticker = {t: salience(text, t) for t in tickers}
        meta = dict(it.metadata)
        meta["salience_per_ticker"] = per_ticker
        out.append(RawItem(
            source=it.source,
            external_id=it.external_id,
            title=it.title,
            url=it.url,
            published_at=it.published_at,
            summary=it.summary,
            body=it.body,
            tickers=tickers,
            metadata=meta,
        ))
    return out


def _to_news_items(items: list[RawItem]) -> list[NewsItem]:
    return [
        NewsItem(
            text=" ".join(filter(None, [it.title, it.summary])),
            ts=it.published_at,
            source=it.source,
            tickers=tuple(it.tickers),
            salience=1.0,
        )
        for it in items
    ]


async def _go(args: argparse.Namespace) -> None:
    since = datetime.now(UTC) - timedelta(hours=args.hours)
    universe = set(args.universe.split(",")) if args.universe else None

    if args.source == "rss":
        async with RssSource() as src:
            raw = await src.fetch(since=since)
    elif args.source == "newsapi":
        async with NewsApiSource(query=args.query) as src:
            raw = await src.fetch(since=since)
    elif args.source == "file":
        src = FileSource(args.path)
        raw = await src.fetch(since=since)
    else:
        raise SystemExit(f"unknown source: {args.source}")

    enriched = _enrich(raw, universe=universe)
    log.info("news.refresh.enriched", in_=len(raw), kept=len(enriched))

    if not enriched:
        return

    scored = LexiconScorer().score(_to_news_items(enriched))
    n = await persist(enriched, scored, scorer_name="lexicon")
    log.info("news.refresh.done", persisted=n)


def main() -> None:
    from atlas_shared import load_env

    load_env()  # so the NewsAPI source (reads os.environ) sees NEWSAPI_KEY
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=["rss", "newsapi", "file"], default="rss")
    p.add_argument("--query", default="stock OR market OR earnings")
    p.add_argument("--path", default="data/news_seed.jsonl")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--universe", default="", help="Optional comma-separated allowlist of tickers.")
    args = p.parse_args()
    asyncio.run(_go(args))


if __name__ == "__main__":
    main()
