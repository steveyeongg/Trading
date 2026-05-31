"""Regression guard for the Postgres-NUMERIC → frontend-number coercion.

The journal page broke at `e.entry_price.toFixed is not a function` because
NUMERIC columns came through as JSON strings (Decimal → str by FastAPI's
default encoder). `to_jsonable` walks rows and casts Decimal → float so the
TS types' `number` claim is honoured at runtime."""

from __future__ import annotations

import json
from decimal import Decimal

from atlas_shared import to_jsonable


def test_decimal_at_top_level_becomes_float() -> None:
    out = to_jsonable([{"entry_price": Decimal("214.30"), "qty": 100}])
    assert out == [{"entry_price": 214.30, "qty": 100}]
    assert isinstance(out[0]["entry_price"], float)


def test_decimal_nested_in_dict_becomes_float() -> None:
    out = to_jsonable([{"meta": {"avg_cost": Decimal("100.5")}}])
    assert isinstance(out[0]["meta"]["avg_cost"], float)


def test_decimal_inside_list_becomes_float() -> None:
    out = to_jsonable([{"levels": [Decimal("100"), Decimal("110")]}])
    assert out[0]["levels"] == [100.0, 110.0]
    assert all(isinstance(x, float) for x in out[0]["levels"])


def test_non_decimal_values_are_passthrough() -> None:
    rows = [{"symbol": "AAPL", "qty": 10, "filled": True, "tag": None}]
    assert to_jsonable(rows) == rows


def test_output_round_trips_through_default_json_encoder() -> None:
    """If `to_jsonable` did its job, json.dumps will succeed without the
    `default=str` escape hatch — i.e. every value is JSON-native."""
    rows = [{"x": Decimal("1.5"), "nested": {"y": [Decimal("2"), Decimal("3")]}}]
    json.dumps(to_jsonable(rows))   # must not raise
