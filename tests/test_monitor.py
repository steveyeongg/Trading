"""Position monitor: decide_exit logic + ExecuteBody carries protective levels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from execution_service import decide_exit

# --- long ------------------------------------------------------------------


def test_long_stop_hit() -> None:
    assert decide_exit(direction="long", last_price=95.0, stop=96.0, target=110.0) == "stop"


def test_long_target_hit() -> None:
    assert decide_exit(direction="long", last_price=111.0, stop=96.0, target=110.0) == "target"


def test_long_no_exit_in_band() -> None:
    assert decide_exit(direction="long", last_price=100.0, stop=96.0, target=110.0) is None


def test_long_stop_precedes_target_same_bar() -> None:
    # Degenerate inputs where both would trigger — stop wins (worst-case-first).
    assert decide_exit(direction="long", last_price=100.0, stop=120.0, target=90.0) == "stop"


# --- short -----------------------------------------------------------------


def test_short_stop_hit() -> None:
    assert decide_exit(direction="short", last_price=105.0, stop=104.0, target=90.0) == "stop"


def test_short_target_hit() -> None:
    assert decide_exit(direction="short", last_price=89.0, stop=104.0, target=90.0) == "target"


def test_short_no_exit_in_band() -> None:
    assert decide_exit(direction="short", last_price=100.0, stop=104.0, target=90.0) is None


# --- time ------------------------------------------------------------------


def test_time_stop_triggers() -> None:
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    past = now - timedelta(minutes=1)
    assert decide_exit(direction="long", last_price=100.0, stop=None, target=None, now=now, time_stop_at=past) == "time"


def test_time_stop_not_yet() -> None:
    now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    future = now + timedelta(hours=1)
    assert decide_exit(direction="long", last_price=100.0, stop=None, target=None, now=now, time_stop_at=future) is None


def test_no_levels_no_exit() -> None:
    assert decide_exit(direction="long", last_price=100.0, stop=None, target=None) is None


# --- route contract --------------------------------------------------------


def test_execute_body_accepts_protective_levels() -> None:
    from signal_service.execution_routes import ExecuteBody

    b = ExecuteBody(symbol="AAPL", quantity=10, stop=206.0, target=222.0, direction="long")
    assert b.stop == 206.0 and b.target == 222.0 and b.direction == "long"
