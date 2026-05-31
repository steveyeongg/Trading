"""Event-driven backtest simulator.

Walks bars in chronological order. For each bar (per symbol):
  1. If a trade is open: check stop, partial-target ladder, time stop.
  2. If flat: ask the strategy for an Order.
  3. Mark-to-market the equity curve at the bar close.

Limitations called out for honesty:
  - Single-bar fills (no L2 queue simulation).
  - Cash isn't actually decremented to buy/sell — positions are sized as
    fractions of equity and PnL is added back. Good enough for long-only
    swing horizons; not for margin-intensive intraday.
  - Multi-symbol simulation iterates symbols *inside* each bar timestamp —
    fine when bar grids align.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
from atlas_shared.logging import get_logger

from backtest_service import metrics as bt_metrics
from backtest_service.strategies.base import Order, Strategy
from backtest_service.types import (
    BacktestConfig,
    BacktestResult,
    ExitReason,
    Trade,
    TradeSide,
)

log = get_logger("backtest.sim")


def _slippage_price(entry_or_exit: str, side: TradeSide, price: float, bps: float) -> float:
    """Adverse slippage: pay up on entries, lose on exits."""
    bps_frac = bps / 10_000.0
    sign = 1.0
    if side == TradeSide.LONG:
        sign = 1.0 if entry_or_exit == "entry" else -1.0
    else:  # SHORT
        sign = -1.0 if entry_or_exit == "entry" else 1.0
    return price * (1.0 + sign * bps_frac)


def _entry_cost(order: Order, cfg: BacktestConfig) -> tuple[float, float]:
    """Apply slippage + commission for an entry fill."""
    fill = _slippage_price("entry", order.side, order.entry_price, cfg.slippage_bps * cfg.cost_multiplier)
    fee = abs(order.qty) * fill * cfg.commission_per_unit * cfg.cost_multiplier
    return fill, fee


def _exit_cost(side: TradeSide, qty: float, price: float, cfg: BacktestConfig) -> tuple[float, float]:
    fill = _slippage_price("exit", side, price, cfg.slippage_bps * cfg.cost_multiplier)
    fee = abs(qty) * fill * cfg.commission_per_unit * cfg.cost_multiplier
    return fill, fee


def _check_exits(
    trade: Trade,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    bar_ts,
    cfg: BacktestConfig,
) -> tuple[ExitReason, float] | None:
    """Decide if this bar closes (any portion of) the trade.

    Priority: stop > targets > time. Worst-case-first to avoid overstating PnL
    when both a stop and a target are hit in the same bar.
    """
    # Stop check.
    if trade.side == TradeSide.LONG and bar_low <= trade.stop_price:
        return ExitReason.STOP, trade.stop_price
    if trade.side == TradeSide.SHORT and bar_high >= trade.stop_price:
        return ExitReason.STOP, trade.stop_price

    # Target ladder: blended exit at the *last* target hit this bar.
    if trade.targets:
        if trade.side == TradeSide.LONG and bar_high >= trade.targets[-1]:
            return ExitReason.TARGET, trade.targets[-1]
        if trade.side == TradeSide.SHORT and bar_low <= trade.targets[-1]:
            return ExitReason.TARGET, trade.targets[-1]

    # Time stop.
    if trade.bars_held >= cfg.time_stop_bars:
        return ExitReason.TIME, bar_close

    return None


def _open_trade(order: Order, ts, cfg: BacktestConfig) -> Trade:
    fill, fee = _entry_cost(order, cfg)
    return Trade(
        symbol=order.symbol,
        side=order.side,
        entry_ts=ts,
        entry_price=fill,
        qty=order.qty,
        stop_price=order.stop_price,
        targets=order.targets,
        target_allocations=list(order.target_allocations),
        fees_paid=fee,
        notes=order.notes,
    )


def _close_trade(
    trade: Trade,
    ts,
    exit_price: float,
    reason: ExitReason,
    cfg: BacktestConfig,
) -> None:
    fill, fee = _exit_cost(trade.side, trade.qty, exit_price, cfg)
    trade.exit_ts = ts
    trade.exit_price = fill
    trade.exit_reason = reason
    trade.fees_paid += fee
    sign = 1.0 if trade.side == TradeSide.LONG else -1.0
    gross = sign * trade.qty * (fill - trade.entry_price)
    trade.realized_pnl = gross - trade.fees_paid
    if trade.entry_price > 0 and trade.qty > 0:
        trade.realized_return = trade.realized_pnl / (trade.entry_price * abs(trade.qty))


def run_backtest(
    bars_by_symbol: dict[str, pd.DataFrame],
    strategy: Strategy,
    config: BacktestConfig | None = None,
    periods_per_year: int = 252,
) -> BacktestResult:
    """Single-pass event-driven simulation.

    `bars_by_symbol` maps symbol → DataFrame with columns
        ['ts','open','high','low','close','volume', ...]
    sorted by ts ascending. Bar grids must align across symbols.
    """
    cfg = config or BacktestConfig()

    if not bars_by_symbol:
        return BacktestResult(config=cfg, trades=[], equity_curve=[], metrics={})

    # Unified timeline.
    all_ts = sorted({ts for df in bars_by_symbol.values() for ts in df["ts"].tolist()})
    # Pre-index each symbol's bars by ts for O(1) lookup.
    indexed: dict[str, dict] = {}
    for sym, df in bars_by_symbol.items():
        indexed[sym] = {
            "df": df.reset_index(drop=True),
            "by_ts": {row["ts"]: i for i, row in df.iterrows()},
        }

    equity = cfg.initial_capital
    open_trades: dict[str, Trade] = {}
    closed_trades: list[Trade] = []
    equity_curve: list[tuple] = []
    bars_processed = 0

    for ts in all_ts:
        # 1) Manage open trades first.
        for sym, trade in list(open_trades.items()):
            idx = indexed[sym]["by_ts"].get(ts)
            if idx is None:
                continue
            row = indexed[sym]["df"].iloc[idx]
            trade.bars_held += 1
            # Track MFE / MAE.
            run_up = (row["high"] - trade.entry_price) / trade.entry_price if trade.side == TradeSide.LONG else (trade.entry_price - row["low"]) / trade.entry_price
            dd = (trade.entry_price - row["low"]) / trade.entry_price if trade.side == TradeSide.LONG else (row["high"] - trade.entry_price) / trade.entry_price
            trade.max_run_up = max(trade.max_run_up, float(run_up))
            trade.max_drawdown = max(trade.max_drawdown, float(dd))

            exit_outcome = _check_exits(trade, float(row["high"]), float(row["low"]), float(row["close"]), ts, cfg)
            if exit_outcome:
                reason, exit_price = exit_outcome
                _close_trade(trade, ts, exit_price, reason, cfg)
                equity += trade.realized_pnl
                closed_trades.append(trade)
                del open_trades[sym]

        # 2) Mark-to-market equity (close-to-close, no MTM on intra-bar wiggle).
        equity_curve.append((ts, equity))

        # 3) Solicit new orders.
        if len(open_trades) >= cfg.max_open_positions:
            continue
        for sym, payload in indexed.items():
            idx = payload["by_ts"].get(ts)
            if idx is None or sym in open_trades:
                continue
            bars_processed += 1
            history = payload["df"].iloc[: idx + 1]
            order = strategy.on_bar(sym, history, open_trades, equity)
            if order is None:
                continue
            trade = _open_trade(order, ts, cfg)
            equity -= trade.fees_paid   # entry fee deducted immediately
            open_trades[sym] = trade
            if len(open_trades) >= cfg.max_open_positions:
                break

    # Force-close anything still open at end-of-data at last bar's close.
    for sym, trade in list(open_trades.items()):
        last_row = indexed[sym]["df"].iloc[-1]
        _close_trade(trade, last_row["ts"], float(last_row["close"]), ExitReason.EOD, cfg)
        equity += trade.realized_pnl
        closed_trades.append(trade)
    if open_trades and equity_curve:
        equity_curve[-1] = (equity_curve[-1][0], equity)

    metrics = bt_metrics.summarise(equity_curve, closed_trades, periods_per_year=periods_per_year)
    log.info("backtest.complete", n_trades=len(closed_trades), final_equity=equity)
    return BacktestResult(
        config=cfg,
        trades=closed_trades,
        equity_curve=equity_curve,
        metrics=metrics,
        bars_processed=bars_processed,
    )


def cost_sensitivity(
    bars_by_symbol: dict[str, pd.DataFrame],
    strategy: Strategy,
    base_config: BacktestConfig | None = None,
    multipliers: Iterable[float] = (0.0, 1.0, 2.0),
) -> dict[float, BacktestResult]:
    """Re-run the strategy at 0x / 1x / 2x cost. BLUEPRINT §11.3."""
    out: dict[float, BacktestResult] = {}
    for m in multipliers:
        cfg = BacktestConfig(**{**(base_config.__dict__ if base_config else {}), "cost_multiplier": m})
        out[m] = run_backtest(bars_by_symbol, strategy, config=cfg)
    return out
