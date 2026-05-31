"""Ladder exit planning: partial take-profits, break-even, chandelier trail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from execution_service import plan_exit


def _meta(**over):
    m = {
        "direction": "long",
        "entry": 100.0,
        "stop_init": 95.0,   # risk = 5, ATR ≈ 2.78
        "stop": 95.0,
        "atr": 2.78,
        "targets": [105.0, 110.0, 115.0],
        "allocations": [0.40, 0.40, 0.20],
        "targets_hit": 0,
        "initial_qty": 100.0,
        "hwm": 100.0,
        "trail_atr_mult": 3.0,
        "time_stop_at": None,
    }
    m.update(over)
    return m


# --- nothing / in-band -----------------------------------------------------


def test_no_action_in_band() -> None:
    a = plan_exit(meta=_meta(), last_price=102.0, open_qty=100.0)
    assert a.kind == "none"
    assert a.meta["hwm"] == 102.0  # hwm ratchets up


# --- partial ladder --------------------------------------------------------


def test_first_target_partial() -> None:
    a = plan_exit(meta=_meta(), last_price=105.5, open_qty=100.0)
    assert a.kind == "partial"
    assert a.qty == 40.0  # 0.40 * 100 initial
    assert a.reason == "target"
    assert a.meta["targets_hit"] == 1


def test_second_target_partial_after_first() -> None:
    a = plan_exit(meta=_meta(targets_hit=1), last_price=110.5, open_qty=60.0)
    assert a.kind == "partial"
    assert a.qty == 40.0
    assert a.meta["targets_hit"] == 2


def test_final_target_closes_remainder() -> None:
    a = plan_exit(meta=_meta(targets_hit=2), last_price=115.5, open_qty=20.0)
    assert a.kind == "all"
    assert a.reason == "target"
    assert a.meta["targets_hit"] == 3


# --- break-even + trailing -------------------------------------------------


def test_breakeven_after_first_target() -> None:
    # After 1 rung, in profit at 108 (>1R since risk=5): stop ratchets to at
    # least entry (break-even), then chandelier = hwm - 3*ATR.
    a = plan_exit(meta=_meta(targets_hit=1, stop=95.0), last_price=108.0, open_qty=60.0)
    # hwm=108 → chandelier = 108 - 8.34 = 99.66; break-even floor = 100 → 100 wins.
    assert a.meta["stop"] >= 100.0


def test_chandelier_trails_up() -> None:
    # Deep in profit, no targets config so trailing dominates.
    a = plan_exit(meta=_meta(targets=[], allocations=[], stop=95.0), last_price=120.0, open_qty=100.0)
    # chandelier = 120 - 3*2.78 = 111.66
    assert a.kind == "none"
    assert round(a.meta["stop"], 2) == round(120.0 - 3 * 2.78, 2)


def test_trailing_stop_triggers_exit() -> None:
    # hwm already high (115), price falls back into the trailed stop.
    a = plan_exit(meta=_meta(targets=[], allocations=[], stop=95.0, hwm=115.0), last_price=106.0, open_qty=100.0)
    # chandelier from hwm=115 → 115 - 8.34 = 106.66; last 106 <= 106.66 → exit.
    assert a.kind == "all"
    assert a.reason == "trail"


# --- hard stop / time ------------------------------------------------------


def test_hard_stop() -> None:
    a = plan_exit(meta=_meta(), last_price=94.0, open_qty=100.0)
    assert a.kind == "all"
    assert a.reason == "stop"


def test_time_stop() -> None:
    now = datetime(2024, 6, 1, tzinfo=UTC)
    past = (now - timedelta(minutes=1)).isoformat()
    a = plan_exit(meta=_meta(time_stop_at=past), last_price=101.0, open_qty=100.0, now=now)
    assert a.kind == "all"
    assert a.reason == "time"


# --- short -----------------------------------------------------------------


def test_short_first_target_partial() -> None:
    m = _meta(direction="short", entry=100.0, stop_init=105.0, stop=105.0,
              targets=[95.0, 90.0, 85.0])
    a = plan_exit(meta=m, last_price=94.0, open_qty=100.0)
    assert a.kind == "partial"
    assert a.reason == "target"


def test_short_hard_stop() -> None:
    m = _meta(direction="short", entry=100.0, stop_init=105.0, stop=105.0, targets=[95.0])
    a = plan_exit(meta=m, last_price=106.0, open_qty=100.0)
    assert a.kind == "all"
    assert a.reason == "stop"
