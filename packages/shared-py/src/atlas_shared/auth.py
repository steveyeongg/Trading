"""Authentication — Clerk/Auth0-compatible JWT verification + a dev mode.

Two modes, chosen by `ATLAS_AUTH_MODE`:
  - "jwt"  (prod): verify a bearer token against a JWKS endpoint (set
            `ATLAS_JWKS_URL`, `ATLAS_JWT_ISSUER`, `ATLAS_JWT_AUDIENCE`).
            The tier is read from a configurable claim (default `tier`).
  - "dev"  (default): no IdP needed. Identity comes from `X-Dev-User` /
            `X-Dev-Tier` headers, else a default anonymous free user. Lets the
            dashboard exercise tier gating end-to-end without a real login.

`AuthContext` is what routes consume. Entitlements are derived from the tier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from atlas_shared.entitlements import TIERS, Entitlements, for_tier
from atlas_shared.logging import get_logger

log = get_logger("auth")


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    email: str | None
    tier: str

    @property
    def entitlements(self) -> Entitlements:
        return for_tier(self.tier)


def auth_mode() -> str:
    return os.environ.get("ATLAS_AUTH_MODE", "dev").lower()


# ---- dev mode -------------------------------------------------------------


def _dev_context(dev_user: str | None, dev_tier: str | None) -> AuthContext:
    tier = (dev_tier or os.environ.get("ATLAS_DEV_TIER", "free")).lower()
    if tier not in TIERS:
        tier = "free"
    uid = dev_user or "dev-user"
    return AuthContext(user_id=uid, email=f"{uid}@dev.local", tier=tier)


# ---- jwt mode -------------------------------------------------------------


@lru_cache(maxsize=1)
def _jwk_client():
    import jwt  # PyJWT

    url = os.environ.get("ATLAS_JWKS_URL")
    if not url:
        raise RuntimeError("ATLAS_JWKS_URL not set but auth mode is 'jwt'")
    return jwt.PyJWKClient(url)


def _verify_jwt(token: str) -> AuthContext:
    import jwt  # PyJWT

    tier_claim = os.environ.get("ATLAS_JWT_TIER_CLAIM", "tier")
    signing_key = _jwk_client().get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=os.environ.get("ATLAS_JWT_AUDIENCE"),
        issuer=os.environ.get("ATLAS_JWT_ISSUER"),
        options={"require": ["exp", "sub"]},
    )
    tier = str(claims.get(tier_claim, "free")).lower()
    if tier not in TIERS:
        tier = "free"
    return AuthContext(
        user_id=str(claims["sub"]),
        email=claims.get("email"),
        tier=tier,
    )


# ---- unified resolver -----------------------------------------------------


class AuthError(Exception):
    """Raised when a bearer token is present but invalid (→ 401 at the edge)."""


def resolve_context(
    *,
    authorization: str | None,
    dev_user: str | None = None,
    dev_tier: str | None = None,
) -> AuthContext:
    """Resolve an AuthContext from request headers. Framework-agnostic so it's
    unit-testable without FastAPI."""
    mode = auth_mode()
    if mode == "jwt":
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        try:
            return _verify_jwt(token)
        except AuthError:
            raise
        except Exception as e:
            log.warning("auth.jwt_invalid", error=str(e))
            raise AuthError("invalid token") from e
    # dev mode
    return _dev_context(dev_user, dev_tier)
