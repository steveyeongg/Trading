"""Portfolio context — what's currently held, sector/correlation caps.

Phase 1.5 keeps this in-memory; Phase 2 will materialise it from `positions`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    asset_class: str
    sector: str | None
    notional: float            # $ at current price
    weight_pct: float          # of total equity
    direction: str = "long"    # "long" | "short"


@dataclass
class PortfolioContext:
    equity: float
    positions: list[OpenPosition] = field(default_factory=list)
    # symbol → symbol → rolling correlation (last 60d). 1.0 if missing.
    corr_matrix: dict[str, dict[str, float]] = field(default_factory=dict)
    # Sector exposure ceiling (fraction of equity).
    sector_cap_pct: float = 0.30
    # MDD headroom — pause new long risk if drawdown exceeds this.
    max_drawdown_pct: float = 0.10
    current_drawdown_pct: float = 0.0

    def sector_weight(self, sector: str | None) -> float:
        if sector is None:
            return 0.0
        return sum(p.weight_pct for p in self.positions if p.sector == sector)

    def max_corr_to_book(self, symbol: str) -> float:
        if not self.positions:
            return 0.0
        row = self.corr_matrix.get(symbol, {})
        if not row:
            return 0.0
        return max(
            (abs(row.get(p.symbol, 0.0)) for p in self.positions if p.symbol != symbol),
            default=0.0,
        )

    def has_existing(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.positions)
