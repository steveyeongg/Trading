"""Regression guard for the alert-on-gated-candidate bug.

The stream broadcaster used to only call `evaluate_signal` when the pipeline
published a trade plan — which meant an alert rule set to "composite >= 20"
never fired unless composite also cleared the pipeline's ≥50 confirmation
gate. This gap silently ignored user intent: the UI let you set the rule,
the engine let you save it, but no notification ever arrived.

The pinned contract:
  1. `PipelineResult` surfaces composite_score + direction_implied + sub_scores
     even when signal is None.
  2. `_SUB_METRICS` reflects the current SubScores schema (news/risk, not the
     removed fund/chain slots).
  3. A gated candidate dict is a valid input to `matches()` and fires rules
     that only depend on composite / direction / sub-scores.
"""

from __future__ import annotations

import pytest
from alert_service.engine import AlertEngine
from alert_service.rules import AlertRule, matches, metric_value


def _rule_composite_ge(threshold: float) -> AlertRule:
    return AlertRule(
        id="r1", name="score-crossing", metric="composite", op=">=",
        threshold=threshold, channels=("log",),
    )


def test_composite_rule_matches_gated_candidate_shape() -> None:
    """A minimal gated candidate — no trade plan — still matches a composite rule."""
    gated_candidate = {
        "symbol": "QBTS",
        "composite_score": 31.0,
        "direction": "long",
        "sub_scores": {"tech": 25, "quant": 40},
        "gated": True,
        "no_signal_reason": "composite |31.0| < 50 (need ≥50)",
    }
    rule = _rule_composite_ge(20.0)
    assert matches(rule, gated_candidate) is True


def test_composite_rule_below_threshold_does_not_match() -> None:
    gated_candidate = {"symbol": "AAPL", "composite_score": 12.0, "direction": "long"}
    assert matches(_rule_composite_ge(20.0), gated_candidate) is False


def test_news_and_risk_are_valid_sub_metrics() -> None:
    """Rules keyed on `news` or `risk` must match — pre-Phase-1 they were
    silently rejected because _SUB_METRICS still had fund/chain."""
    signal = {
        "symbol": "AAPL",
        "composite_score": 60,
        "direction": "long",
        "sub_scores": {"news": 45.0, "risk": 30.0},
    }
    assert metric_value(signal, "news") == 45.0
    assert metric_value(signal, "risk") == 30.0

    news_rule = AlertRule(id="n", name="news", metric="news", op=">=", threshold=40)
    risk_rule = AlertRule(id="r", name="risk", metric="risk", op=">=", threshold=25)
    assert matches(news_rule, signal) is True
    assert matches(risk_rule, signal) is True


def test_stale_fund_chain_metrics_no_longer_supported() -> None:
    """The old `fund`/`chain` slots are gone from the SubScores schema; rules
    keyed on them should NOT match anything (falls through the metric_value
    → top-level field lookup, which is absent → None → no match)."""
    signal = {"symbol": "AAPL", "composite_score": 100, "sub_scores": {}}
    assert metric_value(signal, "fund") is None
    assert metric_value(signal, "chain") is None
    assert matches(AlertRule(id="f", name="f", metric="fund", op=">=", threshold=0), signal) is False


@pytest.mark.asyncio
async def test_alert_engine_dispatches_on_gated_candidate() -> None:
    """End-to-end: the engine fires on a gated candidate matching a composite rule."""
    engine = AlertEngine()
    rule = _rule_composite_ge(20.0)
    gated = {
        "symbol": "QBTS", "composite_score": 31.0, "direction": "long",
        "sub_scores": {"tech": 25, "quant": 40}, "gated": True,
    }
    deliveries = await engine.evaluate_signal(gated, [rule], last_price=100.0, regime="unknown")
    # log channel is always available → at least one delivery attempted.
    assert deliveries, "engine must fire on a gated candidate matching the rule"
    assert deliveries[0]["channel"] == "log"


@pytest.mark.asyncio
async def test_alert_engine_respects_threshold_on_gated_candidate() -> None:
    """A gated candidate with composite BELOW the rule threshold must not fire."""
    engine = AlertEngine()
    rule = _rule_composite_ge(40.0)
    gated = {"symbol": "AAPL", "composite_score": 12.0, "direction": "long",
             "sub_scores": {}, "gated": True}
    deliveries = await engine.evaluate_signal(gated, [rule], last_price=100.0)
    assert deliveries == [], "12 < 40 must not fire"
