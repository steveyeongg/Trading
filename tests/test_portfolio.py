"""Portfolio analytics — valuation + risk math with stubbed DB + prices."""

from __future__ import annotations

import pandas as pd
import pytest


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch):
    """Stub `list_positions`, `cash_balance`, and `latest_bars` so the
    analytics math runs without Postgres."""
    import portfolio_service.analytics as analytics
    from portfolio_service.store import PositionRow

    async def _positions(portfolio_id: str | None = None) -> list[PositionRow]:
        return [
            PositionRow("AAPL", "equity", "Information Technology", 50, 200.0),
            PositionRow("XOM", "equity", "Energy", 80, 100.0),
        ]

    async def _cash(portfolio_id: str | None = None) -> float:
        return 10_000.0

    async def _bars(symbol: str, resolution: str = "1m", limit: int = 400) -> pd.DataFrame:
        # AAPL up to 220, XOM down to 95. Flat-ish vol.
        last = {"AAPL": 220.0, "XOM": 95.0}[symbol]
        closes = [last * (1 + 0.001 * ((i % 5) - 2)) for i in range(50)]
        closes[-1] = last
        return pd.DataFrame({"close": closes})

    monkeypatch.setattr(analytics, "list_positions", _positions)
    monkeypatch.setattr(analytics, "cash_balance", _cash)
    monkeypatch.setattr(analytics, "latest_bars", _bars)
    return analytics


async def test_summary_valuation(stubbed) -> None:
    summary = await stubbed.build_summary("default")
    # AAPL: 50 * 220 = 11_000 (cost 10_000 → +1_000)
    # XOM:  80 * 95  = 7_600  (cost 8_000  → -400)
    # invested = 18_600, cash = 10_000, equity = 28_600
    assert summary.invested == pytest.approx(18_600.0, abs=1.0)
    assert summary.equity == pytest.approx(28_600.0, abs=1.0)
    assert summary.unrealized_pnl == pytest.approx(600.0, abs=1.0)
    assert summary.n_positions == 2


async def test_summary_weights_and_sectors(stubbed) -> None:
    summary = await stubbed.build_summary("default")
    # Weights sum to invested/equity (cash isn't a holding).
    total_wt = sum(h["weight_pct"] for h in summary.holdings)
    assert total_wt == pytest.approx(18_600.0 / 28_600.0 * 100.0, abs=0.5)
    # Two IT? No — AAPL is IT, XOM is Energy.
    assert set(summary.sector_exposure) == {"Information Technology", "Energy"}


async def test_var_is_positive(stubbed) -> None:
    summary = await stubbed.build_summary("default")
    assert summary.var_95 >= 0.0
    assert 0.0 <= summary.var_95_pct <= 1.0
