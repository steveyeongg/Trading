"""Journal: Trade→row conversion + attribution math (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime

from backtest_service.types import ExitReason, Trade, TradeSide
from journal_service import summarise, trade_to_row


def _mk_trade(pnl: float, entry: float = 100.0, stop: float = 98.0, qty: float = 10.0) -> Trade:
    t = Trade(
        symbol="AAPL",
        side=TradeSide.LONG,
        entry_ts=datetime(2024, 1, 2, tzinfo=UTC),
        entry_price=entry,
        qty=qty,
        stop_price=stop,
        targets=[104.0],
        target_allocations=[1.0],
    )
    t.exit_ts = datetime(2024, 1, 3, tzinfo=UTC)
    t.exit_price = entry + pnl / qty
    t.realized_pnl = pnl
    t.realized_return = pnl / (entry * qty)
    t.exit_reason = ExitReason.TARGET if pnl > 0 else ExitReason.STOP
    t.bars_held = 20
    return t


def test_trade_to_row_r_multiple() -> None:
    # entry 100, stop 98 → initial risk = 2 * 10 = 20.
    # +40 PnL → r_multiple = 40 / 20 = 2.0.
    row = trade_to_row(_mk_trade(40.0), strategy="trend-follower")
    assert row["symbol"] == "AAPL"
    assert row["side"] == "long"
    assert row["r_multiple"] == 2.0
    assert row["exit_reason"] == "target"
    assert row["strategy"] == "trend-follower"


def test_trade_to_row_loss_r_multiple() -> None:
    row = trade_to_row(_mk_trade(-20.0), strategy="x")
    assert row["r_multiple"] == -1.0  # lost exactly the initial risk
    assert row["exit_reason"] == "stop"


def test_summarise_empty() -> None:
    s = summarise([])
    assert s["n"] == 0
    assert s["hit_rate"] is None
    assert s["total_pnl"] == 0.0


def test_summarise_mixed() -> None:
    rows = [
        trade_to_row(_mk_trade(40.0), "s"),   # +2R win
        trade_to_row(_mk_trade(-20.0), "s"),  # -1R loss
        trade_to_row(_mk_trade(20.0), "s"),   # +1R win
    ]
    s = summarise(rows)
    assert s["n"] == 3
    assert s["hit_rate"] == 2 / 3
    assert s["avg_win_r"] == 1.5            # (2 + 1) / 2
    assert s["avg_loss_r"] == -1.0
    assert s["expectancy_r"] == (2 - 1 + 1) / 3
    assert s["total_pnl"] == 40.0
    assert s["exit_reasons"]["target"] == 2
    assert s["exit_reasons"]["stop"] == 1
    assert s["by_symbol"]["AAPL"]["n"] == 3
