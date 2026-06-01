"""Explanation engine: templated path always works; LLM path skipped if no key."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from atlas_shared.schemas import (
    AssetClass,
    Conviction,
    Direction,
    Horizon,
    Signal,
    SubScores,
)
from explanation_engine import ExplanationWriter, generate_rationale, templated_rationale


@pytest.fixture
def signal() -> Signal:
    return Signal(
        id=uuid4(),
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        generated_at=datetime.now(UTC),
        horizon=Horizon.SWING,
        direction=Direction.LONG,
        composite_score=78.0,
        confidence_pct=72.0,
        conviction=Conviction.HIGH,
        regime="risk-on",
        sub_scores=SubScores(tech=22, quant=20, sent=12, opt=11, macro=8, news=5, liq=5, risk=4),
        entry_price=214.30,
        stop_price=206.10,
        take_profit_levels=[222.5, 230.0, 238.0],
        position_size_pct=2.1,
        expected_rr=2.4,
        rationale_md=None,
        model_versions={"trend": "v1"},
    )


def test_templated_rationale_format(signal: Signal) -> None:
    """BLUEPRINT §10.3 sections must all appear in the markdown render."""
    md = templated_rationale(signal, features={"rsi14": 58.0, "macd_hist": 0.5})
    assert "SIGNAL: AAPL — LONG" in md
    assert "Composite: +78.0" in md
    assert "Confidence: 72%" in md
    assert "$214.30" in md
    assert "Trade plan" in md
    assert "Summary" in md
    assert "Bull case" in md
    assert "Bear case" in md
    assert "Why entry" in md
    assert "Why stop" in md
    assert "Target logic" in md
    assert "Disclaimer" in md


def test_templated_handles_missing_fields() -> None:
    minimal = Signal(
        id=uuid4(),
        symbol="X",
        asset_class=AssetClass.EQUITY,
        generated_at=datetime.now(UTC),
        horizon=Horizon.SWING,
        direction=Direction.LONG,
        composite_score=60.0,
        confidence_pct=60.0,
        conviction=Conviction.MEDIUM,
        regime="unknown",
        sub_scores=SubScores(),
        entry_price=None,
        stop_price=None,
        take_profit_levels=[],
        position_size_pct=None,
        expected_rr=None,
    )
    md = templated_rationale(minimal)
    assert "—" in md  # missing values rendered as em-dash
    assert "SIGNAL: X — LONG" in md


def test_writer_unavailable_falls_back_to_templated(signal: Signal, monkeypatch) -> None:
    """When no API key is set, the writer must transparently return the templated output."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    w = ExplanationWriter(api_key=None)
    assert not w.available
    out = w.write(signal)
    assert "SIGNAL: AAPL" in out


def test_module_level_generate_rationale_works_offline(signal: Signal, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # Reset the module-level singleton so it re-checks the env.
    import explanation_engine.writer as w_mod
    w_mod._writer = None
    out = generate_rationale(signal)
    assert "SIGNAL: AAPL" in out


@pytest.mark.skipif(not os.environ.get("DEEPSEEK_API_KEY"), reason="needs DEEPSEEK_API_KEY")
def test_writer_live_llm(signal: Signal) -> None:
    """Smoke test against the real API. Only runs when key is present."""
    w = ExplanationWriter()
    assert w.available
    out = w.write(signal)
    assert "AAPL" in out
    assert len(out) > 200
