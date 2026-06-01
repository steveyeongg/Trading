"""Phase 6 — §12.2 event derivation + §12.3 Telegram format.

Covers all seven triggers in `derive_events` and the layout of the
`format_alert` body that Telegram sends.
"""

from __future__ import annotations

from alert_service.channels.base import format_alert
from alert_service.events import derive_events


# ── §12.2 event derivation ────────────────────────────────────────────────────


def _sig(direction: str = "long", composite: float = 70.0, entry: float = 100.0,
         stop: float = 95.0, t1: float = 105.0, t2: float = 110.0, t3: float = 115.0) -> dict:
    return {
        "direction": direction,
        "composite_score": composite,
        "entry_price": entry,
        "stop_price": stop,
        "take_profit_levels": [t1, t2, t3],
    }


def test_signal_new_when_no_prior() -> None:
    e = derive_events(
        current=_sig(), last_price=100.0,
        prior_signal=None, prior_regime=None, current_regime=None,
    )
    assert e.signal_new is True
    assert e.signal_upgraded is False


def test_signal_upgraded_when_composite_climbs() -> None:
    e = derive_events(
        current=_sig(composite=80.0), last_price=100.0,
        prior_signal=_sig(composite=70.0), prior_regime=None, current_regime=None,
    )
    assert e.signal_new is False
    assert e.signal_upgraded is True


def test_signal_not_upgraded_on_small_change() -> None:
    e = derive_events(
        current=_sig(composite=72.0), last_price=100.0,
        prior_signal=_sig(composite=70.0), prior_regime=None, current_regime=None,
    )
    assert e.signal_upgraded is False  # +2 < +5 upgrade threshold


def test_signal_new_on_direction_flip() -> None:
    e = derive_events(
        current=_sig(direction="short"), last_price=100.0,
        prior_signal=_sig(direction="long"), prior_regime=None, current_regime=None,
    )
    assert e.signal_new is True


def test_price_reaches_t1_long() -> None:
    e = derive_events(
        current=_sig(), last_price=106.0,
        prior_signal=_sig(), prior_regime=None, current_regime=None,
    )
    assert e.price_reached_t1 is True
    assert e.price_reached_t2 is False  # 106 < 110
    assert e.price_hit_stop is False


def test_price_reaches_t1_short() -> None:
    e = derive_events(
        current=_sig(direction="short", entry=100, stop=105, t1=95, t2=90, t3=85),
        last_price=94.0,
        prior_signal=_sig(direction="short", entry=100, stop=105, t1=95, t2=90, t3=85),
        prior_regime=None, current_regime=None,
    )
    assert e.price_reached_t1 is True
    assert e.price_hit_stop is False


def test_price_hits_stop_long() -> None:
    e = derive_events(
        current=_sig(), last_price=94.0,
        prior_signal=_sig(), prior_regime=None, current_regime=None,
    )
    assert e.price_hit_stop is True


def test_composite_threshold_crossed() -> None:
    e = derive_events(
        current=_sig(composite=62.0), last_price=100.0,
        prior_signal=_sig(composite=55.0),
        prior_regime=None, current_regime=None,
        composite_threshold=60.0,
    )
    assert e.composite_threshold_crossed is True


def test_macro_regime_changed() -> None:
    e = derive_events(
        current=_sig(), last_price=100.0,
        prior_signal=_sig(),
        prior_regime="risk-on", current_regime="risk-off",
    )
    assert e.macro_regime_changed is True


def test_risk_veto_changed_when_signal_dropped() -> None:
    """A previously published signal that is now vetoed flips veto state."""
    e = derive_events(
        current=None, last_price=100.0,
        prior_signal=_sig(), prior_regime=None, current_regime=None,
    )
    assert e.risk_veto_changed is True


def test_no_spurious_flags_on_steady_state() -> None:
    sig = _sig()
    e = derive_events(
        current=sig, last_price=100.0,
        prior_signal=sig,
        prior_regime="risk-on", current_regime="risk-on",
    )
    assert e.signal_new is False
    assert e.signal_upgraded is False
    assert e.composite_threshold_crossed is False
    assert e.macro_regime_changed is False
    assert e.risk_veto_changed is False
    # last_price 100 == entry → counts as reaching entry for a long.
    assert e.price_reached_entry is True


# ── §12.3 Telegram message format ────────────────────────────────────────────


def test_telegram_format_includes_required_sections() -> None:
    payload = {
        "symbol": "AAPL",
        "direction": "long",
        "composite": 78.0,
        "confidence": 72.0,
        "conviction": "high",
        "entry_price": 214.30,
        "stop_price": 206.10,
        "take_profit_levels": [222.50, 230.00, 238.00],
        "expected_rr": 2.4,
        "invalidations": [
            "Close below 206.10 (stop loss)",
            "Macro regime flips to risk-off",
        ],
        "why_lines": [
            "EMA stack & trend block: +60",
            "Quant trend model: +55",
        ],
        "events": {"signal_new": True, "composite_threshold_crossed": True},
    }
    title, body = format_alert(payload)

    assert title.startswith("🚨 ATLAS Signal: AAPL")
    assert "LONG" in title

    assert "Score: 78.00" in body
    assert "Confidence: 72.00%" in body
    assert "Conviction: HIGH" in body
    assert "Entry: 214.30" in body
    assert "SL: 206.10" in body
    assert "T1: 222.50" in body and "T2: 230.00" in body and "T3: 238.00" in body
    assert "R:R: 1:2.40" in body
    assert "Why:" in body
    assert "Invalidation:" in body
    assert "Close below 206.10" in body
    assert "Informational only" in body
    # Events line should mention the firing triggers.
    assert "signal new" in body or "composite threshold crossed" in body


def test_telegram_format_renders_dashes_for_missing_values() -> None:
    """A vetoed-then-fired alert may lack some fields; renderer must not crash."""
    title, body = format_alert({"symbol": "X", "direction": "long"})
    assert "🚨 ATLAS Signal: X LONG" in title
    assert "Entry: —" in body
    assert "SL: —" in body
    assert "R:R: 1:—" in body
