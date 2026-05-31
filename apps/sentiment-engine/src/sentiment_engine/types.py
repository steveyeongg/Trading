"""Sentiment data classes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewsItem:
    """A single text artifact scoring is requested for."""

    text: str
    ts: datetime
    source: str = "unknown"
    tickers: tuple[str, ...] = ()
    salience: float = 1.0   # 0..1, e.g. confidence the article is actually about the ticker


@dataclass(frozen=True)
class ScoredItem:
    item: NewsItem
    score: float        # -1..+1
    confidence: float   # 0..1
