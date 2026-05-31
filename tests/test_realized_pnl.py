"""Direction-aware realized PnL — the testable core of position closing."""

from __future__ import annotations

from portfolio_service import realized_pnl


def test_long_profit_on_rise() -> None:
    # bought 100 @ 100, sell @ 110 → +1000
    assert realized_pnl("long", avg_cost=100.0, exit_price=110.0, qty=100.0) == 1000.0


def test_long_loss_on_fall() -> None:
    assert realized_pnl("long", avg_cost=100.0, exit_price=95.0, qty=100.0) == -500.0


def test_short_profit_on_fall() -> None:
    # shorted 100 @ 100, cover @ 90 → +1000
    assert realized_pnl("short", avg_cost=100.0, exit_price=90.0, qty=100.0) == 1000.0


def test_short_loss_on_rise() -> None:
    # shorted 100 @ 100, cover @ 105 → -500
    assert realized_pnl("short", avg_cost=100.0, exit_price=105.0, qty=100.0) == -500.0


def test_long_and_short_are_mirror() -> None:
    long_pnl = realized_pnl("long", 100.0, 108.0, 50.0)
    short_pnl = realized_pnl("short", 100.0, 108.0, 50.0)
    assert long_pnl == -short_pnl


def test_unknown_direction_defaults_long() -> None:
    assert realized_pnl("flat", 100.0, 110.0, 10.0) == realized_pnl("long", 100.0, 110.0, 10.0)
