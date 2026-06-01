"""Phase 5 — provider status + data freshness + fail-soft baseline.

Hits the pure helpers in `signal_service.providers`. The HTTP routes are
covered by the existing FastAPI smoke tests; we just need to verify the
payload shape and the env-driven configured/available booleans.
"""

from __future__ import annotations

import pytest
from signal_service.providers import provider_status_payload


def test_status_payload_has_required_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clean room — no provider keys.
    for key in (
        "ALPACA_API_KEY", "ALPACA_API_SECRET", "POLYGON_API_KEY",
        "FRED_API_KEY", "NEWSAPI_KEY", "DEEPSEEK_API_KEY",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "ATLAS_WEBHOOK_URL", "POSTGRES_DSN", "DATABASE_URL", "REDIS_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    payload = provider_status_payload()
    assert {"checked_at", "summary", "providers", "policy"} <= set(payload)
    assert "market_data" in payload["providers"]
    assert "macro" in payload["providers"]
    assert "llm" in payload["providers"]
    assert "alerts" in payload["providers"]
    assert "infra" in payload["providers"]

    # Policy block enumerates the §17 fail-soft rules.
    assert payload["policy"]["missing_optional_keys_must_not_crash"] is True
    assert payload["policy"]["missing_deepseek_uses_templated_explanations"] is True


def test_terminal_market_data_fallback_always_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """yfinance + synthetic must always show available — they're the
    last-resort fallback chain. BLUEPRINT §4.4."""
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)

    payload = provider_status_payload()
    market = {p["name"]: p for p in payload["providers"]["market_data"]}
    assert market["yfinance"]["available"] is True
    assert market["synthetic"]["available"] is True
    assert market["polygon"]["available"] is False
    assert market["alpaca"]["available"] is False


def test_summary_counts_match_rows() -> None:
    payload = provider_status_payload()
    total_rows = sum(len(v) for v in payload["providers"].values())
    assert payload["summary"]["total"] == total_rows
    available = sum(
        1 for cat in payload["providers"].values() for r in cat if r["available"]
    )
    assert payload["summary"]["available"] == available


def test_deepseek_marked_with_templated_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    payload = provider_status_payload()
    llm = {p["name"]: p for p in payload["providers"]["llm"]}
    assert "templated rationale" in llm["deepseek"]["fallback"].lower()


# ── Fail-soft end-to-end baseline ────────────────────────────────────────────


def test_pipeline_runs_with_no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the full pipeline (bars → features → quant → score → risk →
    explanation) with EVERY external provider key unset. Must not raise.

    This is the §22 non-negotiable: missing optional keys must not crash."""
    for key in (
        "ALPACA_API_KEY", "ALPACA_API_SECRET", "POLYGON_API_KEY",
        "FRED_API_KEY", "NEWSAPI_KEY", "DEEPSEEK_API_KEY",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    from feature_engine import gbm_bars
    from signal_service.pipeline import run_pipeline

    bars = gbm_bars(n=500, seed=7)
    result = run_pipeline(
        bars=bars,
        symbol="SYN",
        trend_model=None,        # no model registered
        macro_features=None,     # FRED missing → synthetic fallback already
        sentiment_features=None,
        news_features=None,
        options_features=None,
        generate_explanation=True,  # forces the templated path
    )
    # Either it publishes a signal, vetoes, or gates — never raises, never
    # silent None.
    assert result is not None
    if result.signal is None:
        assert result.no_signal_reason is not None or result.veto is not None
    else:
        # A published signal must still have a rationale (templated, since no
        # DEEPSEEK_API_KEY is set).
        assert result.signal.rationale_md is not None
        assert "Informational only" in result.signal.rationale_md
