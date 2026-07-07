"""ATLAS shared utilities."""

from atlas_shared.auth import AuthContext, AuthError, auth_mode, resolve_context
from atlas_shared.config import Settings, get_settings, load_env
from atlas_shared.entitlements import Entitlements, for_tier, within_limit
from atlas_shared.jsonable import to_jsonable
from atlas_shared.logging import get_logger, setup_logging

__all__ = [
    "AuthContext",
    "AuthError",
    "Entitlements",
    "Settings",
    "auth_mode",
    "for_tier",
    "get_logger",
    "get_settings",
    "load_env",
    "resolve_context",
    "setup_logging",
    "to_jsonable",
    "within_limit",
]
