"""Regression guard for the NewsAPI source.

Pins the contract against https://newsapi.org/docs/endpoints/everything so
the two failure modes that were previously invisible can't come back:

  1. URL construction — must hit `/v2/everything`, NOT `/everything`. The
     previous implementation set `httpx.AsyncClient(base_url=…/v2/everything)`
     then called `client.get("/everything")`, which httpx silently rewrites
     to `https://newsapi.org/everything` (no `/v2/`), producing a 404 in
     production and the developer's "why does nothing come back" moment.

  2. JSON-body error status — NewsAPI returns HTTP 200 with
     `{"status":"error","code":"...","message":"..."}` on quota / key issues.
     Relying on `raise_for_status()` alone makes those failures silent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from news_ingest.sources.newsapi import (
    ENDPOINT,
    DEFAULT_MAX_PAGES,
    NewsApiError,
    NewsApiSource,
)


def _ok(articles: list[dict], total: int | None = None) -> dict:
    return {
        "status": "ok",
        "totalResults": total if total is not None else len(articles),
        "articles": articles,
    }


def _article(title: str = "Apple beats earnings", url: str = "https://x/a") -> dict:
    return {
        "source": {"id": "bloomberg", "name": "Bloomberg"},
        "author": "Jane Doe",
        "title": title,
        "description": "summary",
        "url": url,
        "urlToImage": "https://x/img",
        "publishedAt": "2025-01-15T12:34:56Z",
        "content": "body…",
    }


@pytest.mark.asyncio
async def test_request_targets_v2_everything_not_everything() -> None:
    """The whole purpose of this test — guard the path-rewrite bug."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ok([_article()]))

    src = NewsApiSource(api_key="k")
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"X-Api-Key": "k"})
    await src.fetch()
    assert len(captured) >= 1
    # Exact URL — host + path must be the documented endpoint, not /everything.
    assert str(captured[0].url).startswith(ENDPOINT)
    assert "/v2/everything" in str(captured[0].url)


@pytest.mark.asyncio
async def test_authenticates_via_x_api_key_header_not_query_param() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ok([]))

    src = NewsApiSource(api_key="sekrit-123")
    src._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"X-Api-Key": "sekrit-123"}
    )
    await src.fetch()
    assert captured[0].headers.get("X-Api-Key") == "sekrit-123"
    # Key MUST NOT be in the URL query string — that leaks into access logs.
    assert "apiKey=" not in str(captured[0].url)
    assert "sekrit-123" not in str(captured[0].url)


@pytest.mark.asyncio
async def test_treats_status_error_body_as_failure() -> None:
    """NewsAPI returns HTTP 200 + `status:error` on quota / key issues —
    must NOT be silently swallowed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "code": "apiKeyInvalid", "message": "Your API key is invalid."},
        )

    src = NewsApiSource(api_key="bad")
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(NewsApiError) as exc:
        await src.fetch()
    assert exc.value.code == "apiKeyInvalid"


@pytest.mark.asyncio
async def test_no_api_key_returns_empty_not_raises() -> None:
    """Pipeline contract: missing creds degrade gracefully, never crash."""
    src = NewsApiSource(api_key=None)
    items = await src.fetch()
    assert items == []
    assert src.available is False


@pytest.mark.asyncio
async def test_parses_article_fields_into_rawitem() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok([_article("NVDA pops", "https://x/n")]))

    src = NewsApiSource(api_key="k")
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"X-Api-Key": "k"})
    items = await src.fetch()
    assert len(items) == 1
    item = items[0]
    assert item.title == "NVDA pops"
    assert item.url == "https://x/n"
    assert item.external_id == "https://x/n"
    assert item.source == "newsapi/Bloomberg"
    assert item.published_at.year == 2025
    assert item.metadata["author"] == "Jane Doe"
    assert item.metadata["source_id"] == "bloomberg"


@pytest.mark.asyncio
async def test_pagination_walks_until_total_results_consumed() -> None:
    """100 results requested per page; if totalResults says more, walk pages."""
    page_articles = [
        [_article(f"art{i}", f"https://x/{i}") for i in range(100)],
        [_article(f"art{i}", f"https://x/{i}") for i in range(100, 150)],
    ]
    seen_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        seen_pages.append(page)
        idx = page - 1
        if idx >= len(page_articles):
            return httpx.Response(200, json=_ok([], total=150))
        return httpx.Response(200, json=_ok(page_articles[idx], total=150))

    src = NewsApiSource(api_key="k")
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"X-Api-Key": "k"})
    items = await src.fetch()
    assert seen_pages[:2] == [1, 2]
    assert len(items) == 150


@pytest.mark.asyncio
async def test_pagination_capped_at_max_pages() -> None:
    """Belt-and-braces: even if NewsAPI lies about totalResults, we never
    issue more than `max_pages` requests."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=_ok([_article(f"a{call_count['n']}")] * 100, total=10_000))

    src = NewsApiSource(api_key="k", max_pages=3)
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"X-Api-Key": "k"})
    await src.fetch()
    assert call_count["n"] == 3
    assert DEFAULT_MAX_PAGES == 5  # also pin the default while we're here


@pytest.mark.asyncio
async def test_dev_plan_426_on_page_two_is_benign_cap_not_crash() -> None:
    """NewsAPI dev plan returns HTTP 426 Upgrade Required on `page=2`+.
    Historically this bubbled up as `RetryError` after tenacity retried 3×.
    The fix: promote 4xx → NewsApiError (skips retry), treat the
    `planUpgradeRequired` code as a benign cap, and keep whatever page 1
    returned."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            # A full page — triggers the pagination loop to try page 2.
            return httpx.Response(
                200, json=_ok([_article(f"a{i}") for i in range(100)], total=10_000)
            )
        # Real NewsAPI response body is plain text; mimic that (no JSON).
        return httpx.Response(426, text="Upgrade Required")

    src = NewsApiSource(api_key="k")
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"X-Api-Key": "k"})
    items = await src.fetch()
    # Fetch succeeded with page-1 articles; did NOT retry the 426.
    assert len(items) == 100
    assert call_count["n"] == 2, "page 1 + one attempt at page 2, no retry loop"


@pytest.mark.asyncio
async def test_dev_plan_426_with_json_body_uses_body_code() -> None:
    """Some 426s carry a JSON body with `code=maximumResultsReached`. Handle both."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=_ok([_article()] * 100, total=10_000))
        return httpx.Response(
            426,
            json={
                "status": "error",
                "code": "maximumResultsReached",
                "message": "You have reached the maximum number of results for your plan.",
            },
        )

    src = NewsApiSource(api_key="k")
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"X-Api-Key": "k"})
    items = await src.fetch()
    assert len(items) == 100  # benign — page 1 kept


@pytest.mark.asyncio
async def test_http_401_invalid_key_raises_immediately_no_retry() -> None:
    """401 must NOT retry — it's a permanent error, retrying wastes time and
    burns rate limit. This is the general form of the 426 fix."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(401, json={"status": "error", "code": "apiKeyInvalid", "message": "bad"})

    src = NewsApiSource(api_key="bad")
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(NewsApiError) as exc:
        await src.fetch()
    assert exc.value.code == "apiKeyInvalid"
    assert call_count["n"] == 1, "401 must not be retried"


@pytest.mark.asyncio
async def test_from_param_strips_microseconds() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ok([]))

    since = datetime(2025, 1, 15, 12, 34, 56, 789_000, tzinfo=UTC)
    src = NewsApiSource(api_key="k")
    src._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"X-Api-Key": "k"})
    await src.fetch(since=since)
    from_param = captured[0].url.params.get("from")
    assert from_param is not None
    assert "789" not in from_param  # microseconds gone
    assert from_param.startswith("2025-01-15T12:34:56")
