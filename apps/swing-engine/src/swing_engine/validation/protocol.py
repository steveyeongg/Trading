"""The validation protocol's fixed parts: windows, the declared grid, the budget and the ship gate.

Everything a validation run may tune is declared here before it runs (spec §7.8). The in-sample
window is the only place the grid is searched; the out-of-sample window starts the day after it
and is spent at most :data:`MAX_OOS_RUNS` times per setup family. A fresh window opens only
:data:`REFRESH_MONTHS` after the last freeze.

:func:`evaluate_gate` is the ship gate. Its failure messages are user-facing and fixed. Every
comparison is written so that a NaN (no trades, no baseline) fails rather than passes.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from swing_engine.config import BreakoutParams, PullbackParams, SetupParams

IS_START = date(2016, 1, 1)
IS_END = date(2022, 12, 31)
OOS_START = date(2023, 1, 1)
MAX_OOS_RUNS = 3
REFRESH_MONTHS = 12
MIN_IS_TRADES = 100

GRIDS: dict[str, dict[str, list[float]]] = {
    "breakout": {"stop_atr": [1.5, 2.0, 2.5], "trail_atr": [2.5, 3.0, 3.5]},
    "pullback": {"trail_atr": [2.5, 3.0, 3.5], "stop_buffer_atr": [0.25, 0.5]},
}
"""The only in-sample search: every combination of these values, nothing else."""

FAMILIES: tuple[str, ...] = tuple(GRIDS)
DEFAULT_PARAMS: dict[str, SetupParams] = {
    "breakout": BreakoutParams(),
    "pullback": PullbackParams(),
}
"""Each family's predeclared defaults, the base every grid variant is a replacement of."""


@dataclass(frozen=True)
class GateThresholds:
    """What a setup must show out of sample to go Live.

    ``min_profit_factor_low_coverage`` replaces ``min_profit_factor`` when fewer than
    ``coverage_floor`` of the delisted common stocks have data (survivorship is disclosed, and
    paid for with a stricter floor).
    """

    min_expectancy_r: float = 0.0
    min_profit_factor: float = 1.2
    min_profit_factor_low_coverage: float = 1.3
    coverage_floor: float = 0.90
    min_trades: int = 100


@dataclass(frozen=True)
class Verdict:
    passed: bool
    failures: list[str]


def check_family(family: str) -> str:
    """``family`` when it is a known setup family; otherwise ``ValueError``."""
    if family not in GRIDS:
        raise ValueError(f"unknown setup family {family!r}: expected one of {list(FAMILIES)}")
    return family


def grid_variants(family: str) -> list[dict[str, float]]:
    """Every combination in ``GRIDS[family]``, varying the last key fastest (key order)."""
    grid = GRIDS[check_family(family)]
    return [dict(zip(grid, combo, strict=True)) for combo in itertools.product(*grid.values())]


def evaluate_gate(
    oos: Mapping[str, Any],
    baseline_median: float,
    portfolio_max_dd: float,
    spy_max_dd: float,
    coverage: float,
    t: GateThresholds = GateThresholds(),  # noqa: B008  (frozen dataclass)
) -> Verdict:
    """The ship gate on out-of-sample results; the verdict lists every condition that failed.

    ``oos`` holds the trade-level ``n``, ``expectancy_r`` and ``profit_factor``. Both drawdowns
    are NEGATIVE fractions, and "worse" means deeper: ``portfolio_max_dd < spy_max_dd``. A
    ``coverage`` below ``t.coverage_floor`` (or NaN, unmeasured) raises the profit-factor floor.
    """
    x, pf, n = float(oos["expectancy_r"]), float(oos["profit_factor"]), int(oos["n"])
    floor = (
        t.min_profit_factor if coverage >= t.coverage_floor else t.min_profit_factor_low_coverage
    )
    failures = []
    if not x > t.min_expectancy_r:
        failures.append(f"expectancy {x:.2f}R ≤ {t.min_expectancy_r:g}")
    if not pf >= floor:
        failures.append(f"profit factor {pf:.2f} < {floor:.2f}")
    if n < t.min_trades:
        failures.append(f"only {n} trades (need {t.min_trades})")
    if not x > baseline_median:
        failures.append(f"expectancy {x:.2f}R not above random entry {baseline_median:.2f}R")
    if portfolio_max_dd < spy_max_dd:
        failures.append(
            f"drawdown {abs(portfolio_max_dd):.0%} worse than SPY {abs(spy_max_dd):.0%}"
        )
    return Verdict(passed=not failures, failures=failures)


def select_variant(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The in-sample row with the highest ``expectancy_r`` among those with 100+ trades.

    A tie goes to the higher ``profit_factor``, then to the earlier row (grid order).
    """
    eligible = [row for row in rows if row["n"] >= MIN_IS_TRADES]
    if not eligible:
        raise ValueError(f"no variant has ≥{MIN_IS_TRADES} in-sample trades")
    return max(eligible, key=lambda row: (row["expectancy_r"], row["profit_factor"]))
