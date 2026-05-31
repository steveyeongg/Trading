"""NewsAPI.org source — `/v2/everything` endpoint.

Reference: https://newsapi.org/docs/endpoints/everything

Endpoint:
    GET https://newsapi.org/v2/everything
        ?q=<keywords>            # supports quotes, +/-, AND/OR/NOT
        &searchIn=title,description     # narrow to specific fields
        &language=en
        &sortBy=publishedAt              # relevancy | popularity | publishedAt
        &from=<ISO8601>
        &to=<ISO8601>
        &pageSize=100                    # max 100
        &page=N                          # 1-indexed

Auth: `X-Api-Key: <key>` header (also accepted as `apiKey` query param; we
use the header so the key doesn't leak into URL access logs).

Response shape (HTTP 200 — see the error handling below for the trap):
    {
      "status": "ok",
      "totalResults": 1234,
      "articles": [
        {
          "source": {"id": "bloomberg" | null, "name": "Bloomberg"},
          "author": "...",
          "title": "...",
          "description": "...",
          "url": "https://...",
          "urlToImage": "https://...",
          "publishedAt": "2025-01-15T12:34:56Z",
          "content": "..."   # truncated at ~200 chars on the developer plan
        },
        ...
      ]
    }

⚠️ **Error responses come back as HTTP 200** with body:
    {"status": "error", "code": "rateLimited", "message": "..."}
so we explicitly check `payload["status"]` rather than relying on
`raise_for_status` alone.

Quotas (as of 2024 — verify on the dashboard):
  - Developer (free):  100 req/day, 1 req/sec, articles 24h delayed,
    max 100 results/request.
  - Business+:         no delay, higher rate, ≥250k req/month.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
from atlas_shared.logging import get_logger
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from news_ingest.sources.base import RawItem

log = get_logger("news.newsapi")

ENDPOINT = "https://newsapi.org/v2/everything"
DEFAULT_QUERY = "stock OR market OR earnings"
MAX_PAGE_SIZE = 100              # NewsAPI hard cap per request
DEFAULT_MAX_PAGES = 5            # 5 × 100 = 500 articles per fetch — sane upper bound


class NewsApiError(RuntimeError):
    """Raised on a `status: error` body or HTTP 4xx/5xx that retries can't recover."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class NewsApiSource:
    name = "newsapi"

    def __init__(
        self,
        api_key: str | None = None,
        query: str = DEFAULT_QUERY,
        language: str = "en",
        search_in: str | None = "title,description",
        max_pages: int = DEFAULT_MAX_PAGES,
        timeout: float = 20.0,
    ):
        self.api_key = api_key or os.environ.get("NEWSAPI_KEY")
        self.query = query
        self.language = language
        self.search_in = search_in
        self.max_pages = max(1, int(max_pages))
        # No base_url — we pass the full URL on every call. Setting
        # `base_url=ENDPOINT` plus a relative path can silently rewrite the
        # path (httpx replaces the path component on leading-slash relatives),
        # which is exactly the bug we just fixed.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"X-Api-Key": self.api_key or ""},
        )

    async def __aenter__(self) -> NewsApiSource:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # `NewsApiError` is the application-layer signal that the API itself
    # rejected the request (bad key, exhausted quota, invalid params). Those
    # never self-heal, so we explicitly exclude them from retry — only
    # transient httpx errors (network, 5xx, 429) get the backoff loop.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_not_exception_type(NewsApiError),
    )
    async def _get_page(self, params: dict[str, str | int]) -> dict:
        r = await self._client.get(ENDPOINT, params=params)
        r.raise_for_status()  # 4xx/5xx → httpx.HTTPStatusError → retried by tenacity
        payload = r.json()
        if payload.get("status") != "ok":
            raise NewsApiError(
                payload.get("code", "unknown"),
                payload.get("message", "unspecified error"),
            )
        return payload

    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        if not self.available:
            log.warning("newsapi.skip", reason="no_key")
            return []

        base_params: dict[str, str | int] = {
            "q": self.query,
            "language": self.language,
            "pageSize": MAX_PAGE_SIZE,
            "sortBy": "publishedAt",
        }
        if self.search_in:
            base_params["searchIn"] = self.search_in
        if since:
            # Strip microseconds — NewsAPI accepts both but the simpler form is
            # cleaner in logs and avoids any edge-case parsing surprises.
            base_params["from"] = since.replace(microsecond=0).isoformat()

        items: list[RawItem] = []
        for page in range(1, self.max_pages + 1):
            try:
                payload = await self._get_page({**base_params, "page": page})
            except NewsApiError as e:
                # `maximumResultsReached` is benign — we hit the dev-plan cap.
                # Any other code is worth surfacing.
                if e.code in {"maximumResultsReached"}:
                    log.info("newsapi.cap_reached", page=page, code=e.code)
                    break
                log.warning("newsapi.error", code=e.code, message=e.message, page=page)
                raise

            articles = payload.get("articles") or []
            for a in articles:
                items.append(_article_to_raw_item(a))

            total = int(payload.get("totalResults", 0))
            if len(items) >= total or len(articles) < MAX_PAGE_SIZE:
                break

        log.info("newsapi.fetch.done", n=len(items), pages_used=page)
        return items


def _article_to_raw_item(a: dict) -> RawItem:
    src = (a.get("source") or {})
    return RawItem(
        source=f"newsapi/{src.get('name') or src.get('id') or 'unknown'}",
        external_id=a.get("url"),    # URL is the closest thing to a stable id
        title=a.get("title") or "",
        url=a.get("url"),
        summary=a.get("description"),
        body=a.get("content"),       # truncated on the developer plan
        published_at=_iso(a.get("publishedAt")),
        metadata={
            "author": a.get("author"),
            "url_to_image": a.get("urlToImage"),
            "source_id": src.get("id"),
        },
    )


def _iso(s: str | None) -> datetime:
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
