"""Phase 4 — DeepSeek JSON contract + safety + cache + quant feature_health.

Pure tests — no live LLM call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from atlas_shared.schemas import (
    AssetClass,
    Conviction,
    Direction,
    ExplanationPayload,
    Horizon,
    Signal,
    SubScores,
)
from explanation_engine import (
    ExplanationWriter,
    generate_payload,
    make_cache_key,
    parse_llm_json,
    safety_repair,
    templated_payload,
)
from explanation_engine.payload import DISCLAIMER, render_markdown


def _signal(**over) -> Signal:
    base = dict(
        id=uuid4(),
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        generated_at=datetime.now(UTC),
        horizon=Horizon.SWING,
        direction=Direction.LONG,
        composite_score=72.0,
        confidence_pct=68.0,
        conviction=Conviction.HIGH,
        regime="risk-on",
        sub_scores=SubScores(tech=60, quant=55, news=20),
        entry_price=214.30,
        stop_price=206.10,
        take_profit_levels=[222.5, 230.0, 238.0],
        position_size_pct=2.1,
        expected_rr=2.4,
        rationale_md=None,
        invalidations=["Close below 206.10", "Macro regime flips risk-off"],
        quant_meta={"p_up": 0.71, "feature_health": "ok"},
    )
    base.update(over)
    return Signal(**base)


# ── §10.3 JSON contract — parse + structure ──────────────────────────────────


def test_parse_clean_json() -> None:
    raw = """
    {
      "summary": "Strong setup.",
      "bull_case": ["A", "B"],
      "bear_case": ["X"],
      "why_entry": "now",
      "why_stop": "atr",
      "target_logic": "1R/2R/3R",
      "confidence_comment": "model agrees",
      "final_view": "long"
    }
    """
    payload = parse_llm_json(raw)
    assert isinstance(payload, ExplanationPayload)
    assert payload.summary == "Strong setup."
    assert payload.bull_case == ["A", "B"]


def test_parse_strips_code_fence() -> None:
    raw = '```json\n{"summary": "ok", "bull_case": [], "bear_case": [], "why_entry": "", "why_stop": "", "target_logic": "", "confidence_comment": "", "final_view": ""}\n```'
    payload = parse_llm_json(raw)
    assert payload is not None
    assert payload.summary == "ok"


def test_parse_malformed_returns_none() -> None:
    assert parse_llm_json("not json at all") is None
    assert parse_llm_json("{ this is broken") is None


# ── §10.4 safety rules ────────────────────────────────────────────────────────


def test_safety_strips_guaranteed_language() -> None:
    raw = ExplanationPayload(
        summary="This is a guaranteed winner.",
        bull_case=["The setup is risk-free for swing traders."],
        bear_case=[],
        why_entry="",
        why_stop="",
        target_logic="",
        confidence_comment="A sure thing.",
        final_view="Can't lose at these levels.",
    )
    repaired = safety_repair(raw, _signal())
    blob = " ".join([
        repaired.summary,
        repaired.confidence_comment,
        repaired.final_view,
        " ".join(repaired.bull_case),
    ]).lower()
    assert "guaranteed" not in blob
    assert "risk-free" not in blob
    assert "no risk" not in blob
    assert "sure thing" not in blob
    assert "can't lose" not in blob
    assert repaired.safety_repaired is True


def test_safety_forces_invalidation_when_missing() -> None:
    """§22 #5 — bear_case must always have an invalidation."""
    raw = ExplanationPayload(
        summary="Clean long.",
        bull_case=["Strong trend."],
        bear_case=[],  # <— empty
    )
    repaired = safety_repair(raw, _signal())
    assert len(repaired.bear_case) >= 1
    assert any("Invalidation" in b for b in repaired.bear_case)
    assert repaired.safety_repaired is True


def test_safety_pass_through_clean_payload() -> None:
    clean = ExplanationPayload(
        summary="Reasonable bullish read.",
        bull_case=["Trend lined up."],
        bear_case=["Invalidation: close below stop."],
    )
    repaired = safety_repair(clean, _signal())
    assert repaired.safety_repaired is False
    assert repaired.summary == clean.summary


# ── §10.5 cache key ──────────────────────────────────────────────────────────


def test_cache_key_stable_across_close_composite_scores() -> None:
    """Composite 73 and 74 round to the same bucket (5) → same cache key."""
    s1 = _signal(composite_score=73.0)
    s2 = _signal(composite_score=74.0)
    feats = {"rsi14": 55.0, "macd_hist": 0.5}
    assert make_cache_key(s1, feats) == make_cache_key(s2, feats)


def test_cache_key_differs_across_buckets() -> None:
    s1 = _signal(composite_score=72.0)  # bucket 70
    s2 = _signal(composite_score=78.0)  # bucket 80
    feats = {"rsi14": 55.0}
    assert make_cache_key(s1, feats) != make_cache_key(s2, feats)


def test_cache_key_includes_indicator_hash() -> None:
    s = _signal()
    a = make_cache_key(s, {"rsi14": 55.0})
    b = make_cache_key(s, {"rsi14": 80.0})
    assert a != b


def test_cache_key_includes_news_hash() -> None:
    a = make_cache_key(_signal(sub_scores=SubScores(tech=60, news=10)), {})
    b = make_cache_key(_signal(sub_scores=SubScores(tech=60, news=-40)), {})
    assert a != b


def test_writer_cache_returns_same_payload_for_repeat_call() -> None:
    """Without an API key the writer uses the templated path. The cache
    should still hit on identical input — proves the cache fronts the
    templated branch too, which matters for throughput when scanning."""
    writer = ExplanationWriter(api_key=None)
    sig = _signal()
    first = writer.write_payload(sig, {"rsi14": 55.0})
    second = writer.write_payload(sig, {"rsi14": 55.0})
    # Same object — confirms the cache returned the cached instance, not a re-render.
    assert first is second


def test_writer_cache_bypassed_when_disabled() -> None:
    writer = ExplanationWriter(api_key=None)
    sig = _signal()
    a = writer.write_payload(sig, {"rsi14": 55.0}, use_cache=False)
    b = writer.write_payload(sig, {"rsi14": 55.0}, use_cache=False)
    assert a is not b  # fresh payload each time


# ── Templated payload + markdown render ──────────────────────────────────────


def test_templated_payload_contains_required_sections() -> None:
    payload = templated_payload(_signal())
    assert payload.source == "templated"
    assert payload.summary
    assert payload.bull_case  # supported direction generates bullets
    assert payload.bear_case
    assert any("Invalidation" in b for b in payload.bear_case)


def test_markdown_render_includes_disclaimer_and_title() -> None:
    payload = templated_payload(_signal())
    md = render_markdown(payload, _signal())
    assert "SIGNAL: AAPL — LONG" in md
    assert DISCLAIMER in md
    assert "## Summary" in md
    assert "## Bull case" in md
    assert "## Bear case" in md


def test_generate_rationale_smoke() -> None:
    """Module-level convenience path works end-to-end without an API key."""
    from explanation_engine import generate_rationale
    md = generate_rationale(_signal(), {"rsi14": 55.0})
    assert "SIGNAL: AAPL — LONG" in md
    assert DISCLAIMER in md


# ── §6.2 feature_health on quant_meta ────────────────────────────────────────


def test_quant_meta_present_on_signal_when_provided() -> None:
    sig = _signal(quant_meta={
        "p_up": 0.71,
        "p_down": 0.29,
        "s_quant": 42.0,
        "model_version": "trend/v1",
        "calibrated": True,
        "feature_health": "ok",
    })
    assert sig.quant_meta["feature_health"] == "ok"
    assert sig.quant_meta["p_up"] == 0.71
    assert sig.quant_meta["calibrated"] is True


def test_templated_mentions_degraded_feature_health() -> None:
    sig = _signal(quant_meta={"p_up": 0.62, "feature_health": "degraded"})
    payload = templated_payload(sig)
    assert "feature_health" in payload.confidence_comment.lower() or "degraded" in payload.confidence_comment.lower()
