"""ATLAS sentiment engine."""

from sentiment_engine.aggregator import aggregate, trailing_window
from sentiment_engine.lexicon import LexiconScorer, score_text
from sentiment_engine.score import s_sent
from sentiment_engine.types import NewsItem, ScoredItem

__all__ = [
    "LexiconScorer",
    "NewsItem",
    "ScoredItem",
    "aggregate",
    "s_sent",
    "score_text",
    "trailing_window",
]
