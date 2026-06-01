"""Watchlist persistence + routes — per user.

One watchlist row per user_id. New users with no row get the fallback list on
read. Reads degrade to the fallback when Postgres is unreachable so the
dashboard still renders.
"""

from __future__ import annotations

from atlas_shared import AuthContext, within_limit
from atlas_shared.db import session_scope
from atlas_shared.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from signal_service.auth_dep import current_user

log = get_logger("watchlist")
router = APIRouter(prefix="/v1")

# BLUEPRINT §4.3 default. Equities + index ETFs only — crypto pipeline deferred.
FALLBACK_SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA", "SPY", "QQQ"]

# Light sanity bound — keeps a fat-fingered paste from creating thousands of
# live subscriptions on the WS broadcaster.
MAX_SYMBOLS = 50


class WatchlistBody(BaseModel):
    symbols: list[str] = Field(default_factory=list)


def _clean(symbols: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        sym = s.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out[:MAX_SYMBOLS]


async def get_symbols(user_id: str) -> list[str]:
    async with session_scope() as s:
        row = (
            await s.execute(
                text("SELECT symbols FROM watchlists WHERE user_id = :uid"), {"uid": user_id}
            )
        ).scalar_one_or_none()
    return list(row) if row else FALLBACK_SYMBOLS


async def set_symbols(user_id: str, symbols: list[str]) -> list[str]:
    cleaned = _clean(symbols)
    async with session_scope() as s:
        await s.execute(
            text(
                """
                INSERT INTO watchlists (name, symbols, user_id, updated_at)
                VALUES ('Default', :symbols, :uid, now())
                ON CONFLICT (user_id) DO UPDATE SET symbols = :symbols, updated_at = now()
                """
            ),
            {"uid": user_id, "symbols": cleaned},
        )
    return cleaned


@router.get("/watchlist")
async def read_watchlist(user: AuthContext = Depends(current_user)) -> dict:
    """The current user's watchlist. Falls back to a default list (200, never
    errors) so the dashboard renders even with no DB."""
    try:
        return {"symbols": await get_symbols(user.user_id)}
    except Exception as e:
        log.warning("watchlist.read_fallback", error=str(e))
        return {"symbols": FALLBACK_SYMBOLS, "fallback": True}


@router.put("/watchlist")
async def update_watchlist(body: WatchlistBody, user: AuthContext = Depends(current_user)) -> dict:
    # Tier cap (BLUEPRINT §17.1): free 10, pro 100, elite 500, quant+ unlimited.
    cap = user.entitlements.watchlist_size
    cleaned = _clean(body.symbols)
    if not within_limit(len(cleaned), cap):
        raise HTTPException(
            403,
            f"watchlist limit for tier '{user.tier}' is {cap}; got {len(cleaned)}. Upgrade to add more.",
        )
    try:
        return {"symbols": await set_symbols(user.user_id, cleaned)}
    except Exception as e:
        log.warning("watchlist.write_failed", error=str(e))
        raise HTTPException(503, "watchlist store unavailable") from e
