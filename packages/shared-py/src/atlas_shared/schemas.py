"""Cross-service Pydantic schemas. Single source of truth for inter-service shapes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    FX = "fx"
    FUTURE = "future"
    OPTION = "option"


class Horizon(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"
    POSITION = "position"
    LONG_TERM = "long-term"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class Conviction(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very-high"


class Bar(BaseModel):
    symbol: str
    resolution: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    trade_count: int | None = None


class SubScores(BaseModel):
    tech: float = 0.0
    quant: float = 0.0
    fund: float = 0.0
    macro: float = 0.0
    sent: float = 0.0
    opt: float = 0.0
    liq: float = 0.0
    chain: float = 0.0


class Signal(BaseModel):
    id: UUID
    symbol: str
    asset_class: AssetClass
    generated_at: datetime
    horizon: Horizon
    direction: Direction
    composite_score: float = Field(ge=-100, le=100)
    confidence_pct: float = Field(ge=0, le=100)
    conviction: Conviction
    regime: str
    sub_scores: SubScores
    entry_price: float | None = None
    stop_price: float | None = None
    take_profit_levels: list[float] = Field(default_factory=list)
    position_size_pct: float | None = None
    expected_rr: float | None = None
    rationale_md: str | None = None
    model_versions: dict[str, str] = Field(default_factory=dict)
