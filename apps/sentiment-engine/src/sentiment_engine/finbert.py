"""Optional FinBERT scorer. Lazy-loads `transformers` + `torch`.

Install with: `uv sync --extra finbert`
"""

from __future__ import annotations

from atlas_shared.logging import get_logger

from sentiment_engine.lexicon import LexiconScorer
from sentiment_engine.types import NewsItem, ScoredItem

log = get_logger("sentiment.finbert")

_MODEL_NAME = "ProsusAI/finbert"


class FinBertScorer:
    """ProsusAI/finbert via HuggingFace pipeline.

    Heavy (~440MB download) — only load when the operator actually wants
    nuanced sentiment. The constructor is fail-soft: if transformers/torch
    aren't installed, it logs a warning and falls back to the lexicon.
    """

    name = "finbert"

    def __init__(self):
        self._pipeline = None
        self._fallback = LexiconScorer()
        try:
            from transformers import pipeline  # type: ignore

            self._pipeline = pipeline(
                "sentiment-analysis",
                model=_MODEL_NAME,
                tokenizer=_MODEL_NAME,
                top_k=None,
            )
            log.info("sentiment.finbert.loaded", model=_MODEL_NAME)
        except Exception as e:
            log.warning("sentiment.finbert.unavailable", error=str(e))

    @property
    def available(self) -> bool:
        return self._pipeline is not None

    def score(self, items: list[NewsItem]) -> list[ScoredItem]:
        if not self.available:
            return self._fallback.score(items)
        texts = [it.text for it in items]
        try:
            preds = self._pipeline(texts, truncation=True)
        except Exception as e:
            log.warning("sentiment.finbert.inference_failed", error=str(e))
            return self._fallback.score(items)

        out: list[ScoredItem] = []
        for item, scores in zip(items, preds, strict=False):
            # `scores` is a list of {label, score}. Map: positive→+s, negative→-s, neutral→0.
            net = 0.0
            conf = 0.0
            for entry in scores:
                label = entry["label"].lower()
                p = float(entry["score"])
                conf = max(conf, p)
                if label == "positive":
                    net += p
                elif label == "negative":
                    net -= p
            out.append(ScoredItem(item=item, score=max(-1.0, min(1.0, net)), confidence=conf))
        return out
