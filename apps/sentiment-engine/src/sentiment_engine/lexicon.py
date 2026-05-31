"""Lexicon-based finance sentiment scorer.

A small, Loughran-McDonald-flavoured word list. Not a replacement for FinBERT
on hard cases — it's the offline path that works without HuggingFace downloads
and runs at ~10µs per headline. The full FinBERT scorer lives in finbert.py
and is opt-in.
"""

from __future__ import annotations

import re

from sentiment_engine.types import NewsItem, ScoredItem

_POSITIVE = frozenset(
    ["beat", "beats", "beating", "exceed", "exceeds", "exceeded", "outperform", "outperforms", "outperformed", "upgrade", "upgrades", "upgraded", "raise", "raised", "raises", "raising", "boost", "boosts", "boosted", "growth", "grow", "grows", "growing", "surge", "surges", "surged", "rally", "rallies", "rallied", "gain", "gains", "gained", "record", "records", "high", "highs", "strong", "stronger", "strongest", "strength", "solid", "robust", "accelerate", "accelerates", "accelerating", "acceleration", "optimistic", "bullish", "profit", "profits", "profitable", "expansion", "expanding", "launch", "launches", "launched", "approve", "approves", "approved", "approval", "breakthrough", "partnership", "partnerships", "acquire", "acquires", "acquired", "acquisition", "buyback", "buybacks", "dividend", "dividends", "winner", "winners"]
)

_NEGATIVE = frozenset(
    ["miss", "misses", "missed", "downgrade", "downgraded", "cut", "cuts", "cutting", "lower", "lowered", "drop", "drops", "dropped", "fall", "falls", "fell", "decline", "declines", "declined", "slump", "slumps", "crash", "crashes", "crashed", "plunge", "plunges", "plunged", "tumble", "tumbles", "tumbled", "weak", "weaker", "weakest", "weakness", "slow", "slowing", "slowdown", "contraction", "contract", "bearish", "loss", "losses", "unprofitable", "layoff", "layoffs", "fire", "firing", "fired", "bankrupt", "bankruptcy", "fraud", "fraudulent", "investigation", "probe", "lawsuit", "subpoena", "recall", "delay", "delayed", "warning", "warns", "warned", "guidance-cut", "profit-warning", "going-concern", "downgraded", "loser", "losers", "selloff", "sell-off"]
)

_INTENSIFIERS = frozenset(
    ["massive", "massively", "huge", "huge", "huge", "significantly", "sharply", "dramatically", "record", "record-breaking", "unprecedented", "sweeping", "major"]
)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z\-']*", text.lower())


def score_text(text: str) -> tuple[float, float]:
    """Return (score in [-1, +1], confidence in [0, 1])."""
    if not text:
        return 0.0, 0.0
    tokens = _tokenize(text)
    if not tokens:
        return 0.0, 0.0
    pos = sum(1 for t in tokens if t in _POSITIVE)
    neg = sum(1 for t in tokens if t in _NEGATIVE)
    intens = sum(1 for t in tokens if t in _INTENSIFIERS)
    if pos == 0 and neg == 0:
        return 0.0, 0.0
    raw = (pos - neg) / max(pos + neg, 1)
    boost = min(1.5, 1.0 + 0.25 * intens)
    score = max(-1.0, min(1.0, raw * boost))
    # Confidence increases with the number of sentiment-bearing tokens.
    confidence = min(1.0, (pos + neg) / 6.0)
    return float(score), float(confidence)


class LexiconScorer:
    """Wraps `score_text` to match the FinBERT scorer interface."""

    name = "lexicon"

    def score(self, items: list[NewsItem]) -> list[ScoredItem]:
        out: list[ScoredItem] = []
        for item in items:
            s, c = score_text(item.text)
            out.append(ScoredItem(item=item, score=s, confidence=c))
        return out
