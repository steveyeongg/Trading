"""Demo strategy: EMA-stack trend follower.

Not for production — it's the lowest-discipline strategy that still produces
trades. Used for harness validation and as a benchmark anyone can read in
under a minute. The point is that the simulator, metrics, and walk-forward
all work; this is not a claim of alpha.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from backtest_service.strategies.base import Order
from backtest_service.types import TradeSide

_LOOKBACK = 220


class TrendFollower:
    """Long when EMA(9) > EMA(21) > EMA(50). Stop at 2x ATR(14)."""

    name = "trend-follower"

    def __init__(self, atr_stop_mult: float = 2.0, atr_target_mult: float = 3.0, risk_pct: float = 0.01):
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult
        self.risk_pct = risk_pct

    def on_bar(
        self,
        symbol: str,
        bars: pd.DataFrame,
        open_positions: dict,
        equity: float,
    ) -> Order | None:
        if len(bars) < _LOOKBACK or symbol in open_positions:
            return None
        close = bars["close"]
        e9 = ta.ema(close, length=9)
        e21 = ta.ema(close, length=21)
        e50 = ta.ema(close, length=50)
        atr = ta.atr(bars["high"], bars["low"], close, length=14)
        if any(s.iloc[-1] != s.iloc[-1] for s in (e9, e21, e50, atr)):  # NaN
            return None
        if not (e9.iloc[-1] > e21.iloc[-1] > e50.iloc[-1]):
            return None

        entry = float(close.iloc[-1])
        a = float(atr.iloc[-1])
        stop = entry - self.atr_stop_mult * a
        target = entry + self.atr_target_mult * a
        risk_per_unit = entry - stop
        if risk_per_unit <= 0:
            return None
        dollar_risk = equity * self.risk_pct
        qty = dollar_risk / risk_per_unit

        return Order(
            symbol=symbol,
            side=TradeSide.LONG,
            qty=qty,
            entry_price=entry,
            stop_price=stop,
            targets=[target],
        )
