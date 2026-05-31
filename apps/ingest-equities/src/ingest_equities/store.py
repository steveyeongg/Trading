"""Bar persistence — async upserts into the `bars` hypertable."""

from __future__ import annotations

import pandas as pd
from atlas_shared.db import session_scope
from atlas_shared.logging import get_logger
from sqlalchemy import text

log = get_logger("ingest.store")

# ON CONFLICT lets us replay backfills idempotently. The natural key is
# (symbol, resolution, ts); see migrations/0001_initial.sql.
_UPSERT_SQL = text(
    """
    INSERT INTO bars (ts, symbol, resolution, open, high, low, close, volume, vwap, trade_count)
    VALUES (:ts, :symbol, :resolution, :open, :high, :low, :close, :volume, :vwap, :trade_count)
    ON CONFLICT (symbol, resolution, ts) DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        vwap = EXCLUDED.vwap,
        trade_count = EXCLUDED.trade_count
    """
)


# Columns the upsert binds. Sources that omit optional ones (vwap, trade_count
# in the synthetic generator) get NULLs rather than a bind-parameter error.
_BAR_FIELDS = ("ts", "symbol", "resolution", "open", "high", "low", "close", "volume", "vwap", "trade_count")


async def upsert_bars(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    # Backfill any missing optional columns with NULL so SQLAlchemy can bind
    # every named parameter regardless of source (synthetic vs. Polygon).
    rows = [
        {field: row.get(field) for field in _BAR_FIELDS}
        for row in df.to_dict(orient="records")
    ]
    async with session_scope() as s:
        await s.execute(_UPSERT_SQL, rows)
    log.info("ingest.upsert", n=len(rows), symbol=df["symbol"].iloc[0])
    return len(rows)


async def latest_bars(symbol: str, resolution: str = "1m", limit: int = 500) -> pd.DataFrame:
    sql = text(
        """
        SELECT ts, symbol, resolution, open, high, low, close, volume, vwap, trade_count
        FROM bars
        WHERE symbol = :symbol AND resolution = :resolution
        ORDER BY ts DESC
        LIMIT :limit
        """
    )
    async with session_scope() as s:
        result = await s.execute(sql, {"symbol": symbol, "resolution": resolution, "limit": limit})
        rows = result.mappings().all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    return df
