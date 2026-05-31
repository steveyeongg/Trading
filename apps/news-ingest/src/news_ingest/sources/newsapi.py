"""NewsAPI.org source. Free tier ~100 req/day, paid plans scale up.

Use for ticker-targeted queries that RSS doesn't cover well.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
from atlas_shared.logging import get_logger
from tenacity import retry, stop_after_attempt, wait_exponential

from news_ingest.sources.base import RawItem

log = get_logger("news.newsapi")
BASE = "https://newsapi.org/v2"


class NewsApiSource:
    name = "newsapi"

    def __init__(self, api_key: str | None = None, query: str = "stock OR market OR earnings", timeout: float = 20.0):
        self.api_key = api_key or os.environ.get("NEWSAPI_KEY")
        self.query = query
        self._client = httpx.AsyncClient(timeout=timeout, base_url=BASE)

    async def __aenter__(self) -> NewsApiSource:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    @property
    def available(self) -> bool:
        return self.api_key is not None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        if not self.available:
            log.warning("newsapi.skip", reason="no_key")
            return []
        params: dict[str, str | int] = {
            "q": self.query,
            "apiKey": self.api_key,
            "language": "en",
            "pageSize": 100,
            "sortBy": "publishedAt",
        }
        if since:
            params["from"] = since.isoformat()
        r = await self._client.get("/everything", params=params)
        r.raise_for_status()
        articles = r.json().get("articles", [])
        items: list[RawItem] = []
        for a in articles:
            ts = _iso(a.get("publishedAt"))
            items.append(RawItem(
                source=f"newsapi/{(a.get('source') or {}).get('name', 'unknown')}",
                external_id=a.get("url"),
                title=a.get("title") or "",
                url=a.get("url"),
                summary=a.get("description"),
                body=a.get("content"),
                published_at=ts,
                metadata={"author": a.get("author")},
            ))
        log.info("newsapi.fetch.done", n=len(items))
        return items


def _iso(s: str | None) -> datetime:
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
