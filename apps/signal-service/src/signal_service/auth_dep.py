"""FastAPI auth dependency + /v1/me.

`current_user` resolves an AuthContext from the request (JWT in prod, dev
headers otherwise). Routes that need tier gating depend on it.
"""

from __future__ import annotations

from dataclasses import asdict

from atlas_shared.auth import AuthContext, AuthError, resolve_context
from fastapi import APIRouter, Depends, Header, HTTPException

router = APIRouter(prefix="/v1")


def current_user(
    authorization: str | None = Header(default=None),
    x_dev_user: str | None = Header(default=None),
    x_dev_tier: str | None = Header(default=None),
) -> AuthContext:
    try:
        return resolve_context(
            authorization=authorization,
            dev_user=x_dev_user,
            dev_tier=x_dev_tier,
        )
    except AuthError as e:
        raise HTTPException(401, str(e)) from e


@router.get("/me")
async def me(user: AuthContext = Depends(current_user)) -> dict:
    """Identity + tier + entitlements for the current request."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "tier": user.tier,
        "entitlements": asdict(user.entitlements),
    }
