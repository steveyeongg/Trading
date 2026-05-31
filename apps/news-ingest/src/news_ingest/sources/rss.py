"""RSS / Atom news source.

Free, no API key, broad coverage. Curated default feed list focused on
financial markets. Caller can pass arbitrary URLs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import feedparser
import httpx
from atlas_shared.logging import get_logger

from news_ingest.sources.base import RawItem

log = get_logger("news.rss")


DEFAULT_FEEDS = [
    # Aggregated business / markets feeds — all free.
    ("yahoo-finance",  "https://finance.yahoo.com/news/rssindex"),
    ("cnbc-top",       "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ("reuters-business", "https://feeds.reuters.com/reuters/businessNews"),
    ("nyt-business",   "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
    ("bbc-business",   "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("aljazeera-econ", "https://www.aljazeera.com/xml/rss/all.xml"),
]


class RssSource:
    name = "rss"

    def __init__(self, feeds: list[tuple[str, str]] | None = None, timeout: float = 20.0):
        self.feeds = feeds or DEFAULT_FEEDS
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def __aenter__(self) -> RssSource:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def _fetch_one(self, source: str, url: str) -> list[RawItem]:
        try:
            r = await self._client.get(url)
            r.raise_for_status()
        except Exception as e:
            log.warning("rss.fetch_failed", source=source, error=str(e))
            return []
        # feedparser is synchronous; the parse itself is fast (<10ms typical).
        feed = feedparser.parse(r.content)
        items: list[RawItem] = []
        for entry in feed.entries:
            ts = _parse_ts(entry)
            items.append(RawItem(
                source=source,
                external_id=entry.get("id") or entry.get("link"),
                title=entry.get("title", ""),
                url=entry.get("link"),
                summary=entry.get("summary"),
                published_at=ts,
                metadata={"feed": source},
            ))
        return items

    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        results = await asyncio.gather(*(self._fetch_one(s, u) for s, u in self.feeds), return_exceptions=False)
        out: list[RawItem] = []
        for batch in results:
            out.extend(batch)
        if since:
            out = [i for i in out if i.published_at >= since]
        log.info("rss.fetch.done", feeds=len(self.feeds), items=len(out))
        return out


def _parse_ts(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        v = entry.get(key)
        if v:
            return datetime(*v[:6], tzinfo=UTC)
    return datetime.now(UTC)
