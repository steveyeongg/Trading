"""News ingest: ticker extraction, file source, retrieval contract."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from news_ingest import FileSource, extract_tickers, salience
from news_ingest.refresh import _enrich, _to_news_items

# --- ticker extraction -----------------------------------------------------


def test_cashtag_detected() -> None:
    assert extract_tickers("Just bought $AAPL and $NVDA today") == ["AAPL", "NVDA"]


def test_alias_detected() -> None:
    syms = extract_tickers("Apple beats expectations as Microsoft warns on cloud")
    assert "AAPL" in syms and "MSFT" in syms


def test_universe_filter() -> None:
    syms = extract_tickers("$AAPL $NVDA Apple Microsoft", universe={"AAPL"})
    assert syms == ["AAPL"]


def test_empty_text_returns_empty() -> None:
    assert extract_tickers("") == []


def test_no_false_positive_on_random_text() -> None:
    assert extract_tickers("The quarterly meeting is scheduled for Tuesday") == []


def test_salience_higher_for_lede_mention() -> None:
    """`salience()` should boost when the ticker appears in the first 60
    characters (likely the subject of the headline)."""
    early = salience("Apple announces record buyback program", "AAPL")
    # Pad enough so 'Apple' lands past character 60.
    late = salience(
        "Markets broadly rose today on dovish Fed commentary and softer macro data; among the gainers Apple traded slightly higher.",
        "AAPL",
    )
    assert early > late


def test_salience_zero_for_missing_ticker() -> None:
    assert salience("Microsoft beats", "AAPL") == 0.0


def test_salience_decays_with_multiple_tickers() -> None:
    """When several tickers are mentioned, each one's salience shrinks."""
    single = salience("Apple raises guidance", "AAPL")
    multi = salience("Apple, Microsoft, Nvidia and Tesla all rose", "AAPL")
    assert single > multi


# --- file source -----------------------------------------------------------


def test_file_source_reads_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "news.jsonl"
    items = [
        {"title": "Apple beats earnings", "url": "https://x/1",
         "published_at": "2026-05-20T10:00:00Z", "source": "test"},
        {"title": "Random unrelated headline", "url": "https://x/2",
         "published_at": "2026-05-20T11:00:00Z", "source": "test"},
    ]
    p.write_text("\n".join(json.dumps(i) for i in items))
    src = FileSource(p)
    out = asyncio.run(src.fetch())
    assert len(out) == 2
    assert out[0].title.startswith("Apple")


def test_file_source_filters_by_since(tmp_path: Path) -> None:
    p = tmp_path / "news.jsonl"
    p.write_text(json.dumps({"title": "x", "published_at": "2023-01-01T00:00:00Z", "source": "t"}))
    src = FileSource(p)
    cutoff = datetime(2024, 1, 1, tzinfo=UTC)
    out = asyncio.run(src.fetch(since=cutoff))
    assert out == []


def test_file_source_missing_path(tmp_path: Path) -> None:
    src = FileSource(tmp_path / "does-not-exist.jsonl")
    out = asyncio.run(src.fetch())
    assert out == []


# --- enrich + score pipeline (in-memory, no DB) ----------------------------


def test_enrich_drops_items_with_no_universe_hit(tmp_path: Path) -> None:
    p = tmp_path / "news.jsonl"
    p.write_text("\n".join([
        json.dumps({"title": "Apple beats guidance", "published_at": "2026-05-20T10:00:00Z", "source": "t"}),
        json.dumps({"title": "Weather forecast for Tuesday", "published_at": "2026-05-20T11:00:00Z", "source": "t"}),
    ]))
    raw = asyncio.run(FileSource(p).fetch())
    enriched = _enrich(raw, universe={"AAPL"})
    assert len(enriched) == 1
    assert enriched[0].tickers == ("AAPL",)


def test_to_news_items_carries_tickers(tmp_path: Path) -> None:
    p = tmp_path / "news.jsonl"
    p.write_text(json.dumps({
        "title": "Apple beats earnings",
        "published_at": "2026-05-20T10:00:00Z",
        "source": "t",
    }))
    raw = asyncio.run(FileSource(p).fetch())
    enriched = _enrich(raw, universe={"AAPL"})
    items = _to_news_items(enriched)
    assert len(items) == 1
    assert "AAPL" in items[0].tickers
    assert "Apple" in items[0].text


# --- retrieval contract (DB-skipped) --------------------------------------


@pytest.mark.skipif(True, reason="requires running Postgres + news_items rows")
def test_retrieval_against_live_db() -> None:
    """Manual integration: refresh news first, then check `recent_sentiment_features`.

    Left as a docstring of the expected return shape:
        {'news_sent_score': float, 'news_sent_confidence': float, 'news_count': float}
    """
    from news_ingest import recent_sentiment_features
    out = asyncio.run(recent_sentiment_features("AAPL", hours=48))
    assert "news_sent_score" in out
    assert 0 <= out["news_sent_confidence"] <= 1
    assert out["news_count"] >= 0
    _ = timedelta(hours=1)
