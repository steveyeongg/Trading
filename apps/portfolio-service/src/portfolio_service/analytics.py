"""Portfolio analytics: holdings valuation + risk strip.

Marks positions to the latest stored bar close, computes weights, sector
exposure, and a parametric portfolio VaR. Realized-vol per name comes from the
bar history; correlation is approximated as the mean pairwise correlation of
recent returns (Phase 3 swaps in the Neo4j correlation graph).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from ingest_equities.store import latest_bars

from portfolio_service.store import PositionRow, cash_balance, list_positions


@dataclass
class HoldingView:
    symbol: str
    asset_class: str
    sector: str | None
    quantity: float
    avg_cost: float
    last_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pct: float
    weight_pct: float


@dataclass
class PortfolioSummary:
    equity: float
    cash: float
    invested: float
    unrealized_pnl: float
    unrealized_pct: float
    n_positions: int
    var_95: float                 # 1-day 95% parametric VaR ($)
    var_95_pct: float
    sector_exposure: dict[str, float]
    holdings: list[dict]


async def _last_price_and_vol(symbol: str) -> tuple[float | None, float]:
    """Return (last_close, daily_return_std). Falls back to (None, 0.0)."""
    bars = await latest_bars(symbol, resolution="1m", limit=400)
    if bars.empty:
        return None, 0.0
    close = bars["close"].to_numpy(dtype=float)
    last = float(close[-1])
    if close.size < 5:
        return last, 0.0
    rets = np.diff(close) / close[:-1]
    return last, float(np.std(rets, ddof=1))


async def build_summary(user_id: str = "dashboard") -> PortfolioSummary:
    positions: list[PositionRow] = await list_positions(user_id)
    cash = await cash_balance(user_id)

    holdings: list[HoldingView] = []
    vols: list[float] = []
    values: list[float] = []
    for p in positions:
        last, vol = await _last_price_and_vol(p.symbol)
        price = last if last is not None else p.avg_cost   # flat if no bars
        mv = p.quantity * price
        cost_basis = p.quantity * p.avg_cost
        upnl = mv - cost_basis
        upct = (upnl / cost_basis) if cost_basis else 0.0
        holdings.append(
            HoldingView(
                symbol=p.symbol,
                asset_class=p.asset_class,
                sector=p.sector,
                quantity=p.quantity,
                avg_cost=p.avg_cost,
                last_price=price,
                market_value=mv,
                unrealized_pnl=upnl,
                unrealized_pct=upct,
                weight_pct=0.0,  # filled below once equity known
            )
        )
        vols.append(vol)
        values.append(mv)

    invested = float(sum(values))
    equity = invested + cash
    total_cost = sum(h.quantity * h.avg_cost for h in holdings)
    total_upnl = float(sum(h.unrealized_pnl for h in holdings))
    total_upct = (total_upnl / total_cost) if total_cost else 0.0

    # Weights.
    for h in holdings:
        h.weight_pct = (h.market_value / equity * 100.0) if equity else 0.0

    # Parametric VaR: assume independence as a conservative-ish first cut
    # (independence understates diversification but overstates concentration
    #  risk — fine as a headline number; CVaR + correlation in Phase 3).
    # Position 1-day vol in $ = mv * daily_std. Portfolio variance = sum of
    # squared position dollar-vols (independence).
    dollar_var = 0.0
    for mv, vol in zip(values, vols, strict=True):
        dollar_var += (mv * vol) ** 2
    sigma_dollars = float(np.sqrt(dollar_var))
    var_95 = 1.645 * sigma_dollars  # one-sided 95% normal quantile
    var_95_pct = (var_95 / equity) if equity else 0.0

    # Sector exposure.
    sector_exposure: dict[str, float] = {}
    for h in holdings:
        key = h.sector or "unknown"
        sector_exposure[key] = sector_exposure.get(key, 0.0) + h.weight_pct

    return PortfolioSummary(
        equity=equity,
        cash=cash,
        invested=invested,
        unrealized_pnl=total_upnl,
        unrealized_pct=total_upct,
        n_positions=len(holdings),
        var_95=var_95,
        var_95_pct=var_95_pct,
        sector_exposure=sector_exposure,
        holdings=[asdict(h) for h in holdings],
    )
