"""Coerce DB rows into shapes that FastAPI's default encoder ships as JSON
numbers, not strings.

Why this exists:
  Postgres `NUMERIC` columns come back through asyncpg as Python `Decimal`.
  FastAPI's encoder serializes `Decimal` as a JSON string (to preserve
  precision). The frontend's TypeScript types declare `number`, so client
  code calling `.toFixed()` on a numeric column blows up at runtime.

`to_jsonable` walks a row (or a list of rows / nested dict) and converts
`Decimal` → `float`. It's a one-liner at every call site that returns rows to
the API."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _coerce(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_coerce(v) for v in value)
    return value


def to_jsonable(rows: list[dict]) -> list[dict]:
    """List-of-dicts variant — the common DB-result case."""
    return [_coerce(row) for row in rows]
