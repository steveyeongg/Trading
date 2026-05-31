"""s_sent — sentiment sub-score in [-100, +100]."""

from __future__ import annotations

from typing import Any

import numpy as np


def _get(d: dict[str, Any], k: str, default: float = 0.0) -> float:
    v = d.get(k)
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(f):
        return default
    return f


def s_sent(sentiment_features: dict[str, Any]) -> float:
    """Combine news, social, and analyst-drift signals into one sub-score.

    Expected keys (all optional, all in [-1, +1] for the *_score fields):
        news_sent_score, news_sent_confidence
        social_sent_score, social_sent_confidence
        analyst_drift        — net upgrades / total ratings actions, [-1, +1]
    """
    w_news = 0.55
    w_social = 0.30
    w_analyst = 0.15

    news = _get(sentiment_features, "news_sent_score") * _get(sentiment_features, "news_sent_confidence", 1.0)
    social = _get(sentiment_features, "social_sent_score") * _get(sentiment_features, "social_sent_confidence", 1.0)
    analyst = _get(sentiment_features, "analyst_drift")

    raw = w_news * news + w_social * social + w_analyst * analyst
    return float(np.clip(raw * 100.0, -100.0, 100.0))
