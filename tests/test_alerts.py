"""Alert engine: rule matching, cooldown, channel dispatch (no DB)."""

from __future__ import annotations

from alert_service import AlertEngine, AlertRule, matches, metric_value


def _signal(**over) -> dict:
    base = {
        "symbol": "AAPL",
        "direction": "long",
        "composite_score": 78.0,
        "confidence_pct": 72.0,
        "sub_scores": {"tech": 22, "quant": 20, "macro": 8, "sent": 12, "liq": 5,
                       "fund": 0, "opt": 0, "chain": 0},
    }
    base.update(over)
    return base


# --- metric extraction -----------------------------------------------------


def test_metric_value_resolves_fields_and_subs() -> None:
    s = _signal()
    assert metric_value(s, "composite") == 78.0
    assert metric_value(s, "confidence") == 72.0
    assert metric_value(s, "tech") == 22.0
    assert metric_value(s, "quant") == 20.0
    assert metric_value(s, "missing") is None


# --- rule matching ---------------------------------------------------------


def test_matches_threshold() -> None:
    r = AlertRule(id="1", name="strong", metric="composite", op=">=", threshold=70)
    assert matches(r, _signal(composite_score=78))
    assert not matches(r, _signal(composite_score=60))


def test_matches_symbol_filter() -> None:
    r = AlertRule(id="1", name="x", metric="composite", op=">=", threshold=70, symbol="MSFT")
    assert not matches(r, _signal(symbol="AAPL"))
    assert matches(r, _signal(symbol="MSFT", composite_score=80))


def test_matches_direction_filter() -> None:
    r = AlertRule(id="1", name="x", metric="composite", op="<=", threshold=-70, direction="short")
    assert matches(r, _signal(direction="short", composite_score=-80))
    assert not matches(r, _signal(direction="long", composite_score=-80))


def test_disabled_rule_never_matches() -> None:
    r = AlertRule(id="1", name="x", metric="composite", op=">=", threshold=0, enabled=False)
    assert not matches(r, _signal())


def test_subscore_rule() -> None:
    r = AlertRule(id="1", name="quant", metric="quant", op=">=", threshold=15)
    assert matches(r, _signal())  # quant=20


# --- engine dispatch + cooldown -------------------------------------------


async def test_engine_dispatches_log_channel() -> None:
    engine = AlertEngine()
    rule = AlertRule(id="r1", name="strong", metric="composite", op=">=", threshold=70, channels=("log",))
    deliveries = await engine.evaluate_signal(_signal(), [rule])
    assert len(deliveries) == 1
    assert deliveries[0]["channel"] == "log"
    assert deliveries[0]["ok"] is True


async def test_engine_respects_cooldown() -> None:
    engine = AlertEngine()
    rule = AlertRule(id="r1", name="x", metric="composite", op=">=", threshold=70, channels=("log",), cooldown_s=3600)
    first = await engine.evaluate_signal(_signal(), [rule])
    second = await engine.evaluate_signal(_signal(), [rule])
    assert len(first) == 1
    assert second == []   # cooled down


async def test_engine_unavailable_channel_records_failure() -> None:
    engine = AlertEngine()
    # telegram has no token in test env → unavailable.
    rule = AlertRule(id="r1", name="x", metric="composite", op=">=", threshold=70, channels=("telegram",))
    out = await engine.evaluate_signal(_signal(), [rule])
    assert len(out) == 1
    assert out[0]["ok"] is False
    assert "unavailable" in out[0]["detail"]


async def test_engine_no_match_no_dispatch() -> None:
    engine = AlertEngine()
    rule = AlertRule(id="r1", name="x", metric="composite", op=">=", threshold=90, channels=("log",))
    assert await engine.evaluate_signal(_signal(composite_score=50), [rule]) == []
