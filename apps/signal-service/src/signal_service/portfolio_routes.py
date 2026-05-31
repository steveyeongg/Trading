"""Portfolio endpoint — scoped to the current user."""

from __future__ import annotations

from dataclasses import asdict

from atlas_shared import AuthContext
from fastapi import APIRouter, Depends
from portfolio_service import build_summary

from signal_service.auth_dep import current_user

router = APIRouter(prefix="/v1")


@router.get("/portfolios/{portfolio_id}")
async def portfolio(portfolio_id: str, user: AuthContext = Depends(current_user)) -> dict:
    """Holdings + risk strip for the current user's portfolio. The path id is
    accepted for REST shape but resolution is by authenticated user — you can
    only see your own."""
    summary = await build_summary(user.user_id)
    return asdict(summary)
