"""Regression guard for the synthetic-bars → bars-table column contract.

Before today's fix, the synthetic GBM generator produced 8 columns but the
upsert SQL bound 10, raising `InvalidRequestError: A value is required for
bind parameter 'vwap'` at runtime. This test asserts every parameter named in
the upsert SQL exists in the normalisation tuple `_BAR_FIELDS`."""

from __future__ import annotations

import re

from feature_engine import gbm_bars
from ingest_equities.store import _BAR_FIELDS, _UPSERT_SQL


def test_bar_fields_covers_every_bind_parameter() -> None:
    """Every `:name` in the upsert SQL must be in _BAR_FIELDS, otherwise we'd
    crash on missing dict keys at execute time."""
    bound = set(re.findall(r":([a-zA-Z_]\w*)", _UPSERT_SQL.text))
    missing = bound - set(_BAR_FIELDS)
    assert not missing, f"upsert SQL binds {missing!r} but _BAR_FIELDS doesn't list them"


def test_synthetic_bars_round_trip_through_normalisation() -> None:
    """The synthetic generator emits a subset of columns; normalisation must
    fill the rest with None so SQLAlchemy can bind."""
    df = gbm_bars(n=3, seed=0, symbol="AAPL")
    rows = [
        {field: row.get(field) for field in _BAR_FIELDS}
        for row in df.to_dict(orient="records")
    ]
    for row in rows:
        # Every bind name from the SQL must be a key (None is fine, missing is not).
        assert set(row.keys()) == set(_BAR_FIELDS)
    # And the optional Polygon-only columns are explicitly None.
    assert all(r["vwap"] is None for r in rows)
    assert all(r["trade_count"] is None for r in rows)
