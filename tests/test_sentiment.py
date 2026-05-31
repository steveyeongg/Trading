"""Sentiment engine: lexicon scoring, aggregation, s_sent sub-score."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sentiment_engine import (
    LexiconScorer,
    NewsItem,
    aggregate,
    s_sent,
    score_text,
    trailing_window,
)


def test_lexicon_positive_text() -> None:
    score, conf = score_text("Apple beats expectations and raises full-year guidance")
    assert score > 0.5
    assert conf > 0.0


def test_lexicon_negative_text() -> None:
    score, conf = score_text("Company misses earnings, slashes guidance, faces lawsuit")
    assert score < -0.5
    assert conf > 0.0


def test_lexicon_neutral_text() -> None:
    score, conf = score_text("The meeting is scheduled for Tuesday")
    assert score == 0.0
    assert conf == 0.0


def test_lexicon_intensifier_amplifies() -> None:
    plain, _ = score_text("Stock surges on strong guidance")
    boosted, _ = score_text("Stock surges massively on record-breaking guidance")
    assert abs(boosted) >= abs(plain)


def test_aggregator_weighted_mean() -> None:
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    items = [
        NewsItem(text="Apple beats and raises", ts=now - timedelta(hours=1), tickers=("AAPL",), salience=1.0),
        NewsItem(text="Apple slumps and misses",  ts=now - timedelta(hours=1), tickers=("AAPL",), salience=1.0),
    ]
    scored = LexiconScorer().score(items)
    agg = aggregate(scored, "AAPL", now=now)
    # Should be roughly neutral.
    assert abs(agg["sent_score"]) < 0.3
    assert agg["sent_count"] == 2.0


def test_aggregator_time_decay() -> None:
    """Older positive items should be outweighed by recent negative ones."""
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    items = [
        NewsItem(text="Apple beats and raises", ts=now - timedelta(hours=48), tickers=("AAPL",)),
        NewsItem(text="Apple slumps and misses", ts=now - timedelta(minutes=10), tickers=("AAPL",)),
    ]
    scored = LexiconScorer().score(items)
    agg = aggregate(scored, "AAPL", now=now, half_life_hours=6.0)
    assert agg["sent_score"] < -0.1


def test_aggregator_ignores_other_tickers() -> None:
    now = datetime(2024, 6, 1, tzinfo=UTC)
    items = [NewsItem(text="Apple beats", ts=now, tickers=("AAPL",))]
    scored = LexiconScorer().score(items)
    agg = aggregate(scored, "MSFT", now=now)
    assert agg["sent_count"] == 0.0
    assert agg["sent_score"] == 0.0


def test_trailing_window_filters_by_age() -> None:
    now = datetime(2024, 6, 1, tzinfo=UTC)
    items = [
        NewsItem(text="x", ts=now - timedelta(hours=2), tickers=("X",)),
        NewsItem(text="y", ts=now - timedelta(hours=48), tickers=("X",)),
    ]
    scored = LexiconScorer().score(items)
    recent = trailing_window(scored, hours=24.0, now=now)
    assert len(recent) == 1


# --- s_sent ----------------------------------------------------------------


def test_s_sent_neutral_on_empty() -> None:
    assert s_sent({}) == 0.0


def test_s_sent_positive_when_all_inputs_bullish() -> None:
    out = s_sent({
        "news_sent_score": 0.7, "news_sent_confidence": 0.9,
        "social_sent_score": 0.5, "social_sent_confidence": 0.8,
        "analyst_drift": 0.6,
    })
    assert out > 30.0


def test_s_sent_negative_when_all_inputs_bearish() -> None:
    out = s_sent({
        "news_sent_score": -0.7, "news_sent_confidence": 0.9,
        "social_sent_score": -0.5, "social_sent_confidence": 0.8,
        "analyst_drift": -0.6,
    })
    assert out < -30.0


@pytest.mark.parametrize("garbage", [None, float("nan"), "not_a_number"])
def test_s_sent_robust_to_garbage_values(garbage) -> None:
    out = s_sent({"news_sent_score": garbage, "social_sent_score": 0.4, "social_sent_confidence": 0.8})
    # garbage should collapse to 0, social bullish → mildly positive.
    assert out > 0.0
