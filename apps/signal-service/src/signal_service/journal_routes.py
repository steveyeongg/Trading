"""Journal endpoint — entries + attribution stats."""

from __future__ import annotations

from fastapi import APIRouter, Query
from journal_service import list_entries, summarise

router = APIRouter(prefix="/v1")


@router.get("/journal")
async def journal(limit: int = Query(200, ge=1, le=1000)) -> dict:
    """Recent journal entries plus aggregate attribution."""
    entries = await list_entries(limit=limit)
    return {"entries": entries, "attribution": summarise(entries)}
