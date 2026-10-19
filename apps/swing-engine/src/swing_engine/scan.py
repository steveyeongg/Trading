"""The nightly scan: tonight's ranked swing ideas, sized, and what to leave alone.

:func:`build_context` truncates the panel to the scan session first and only then computes
indicators, universe eligibility, relative strength, leaders and SPY's regime, so nothing
after the session can reach an idea. :func:`run_scan` is built on it: each setup spec yields
its candidates dated the session, each is sized at the regime factor and ranked, and the
result also reports how much of the book is already in use.

Regime gating (a spec's ``regime_gate``): a gated spec is sized at the market factor
(1.0 uptrend, 0.5 mixed, 0.0 downtrend) and in a downtrend its ideas are parked in
``watch_only`` at zero shares. An ungated spec, one whose regime filter did not help out of
sample, keeps its ideas in ``ideas`` at factor 1.0 in every regime, with ``regime_warning`` set
whenever the market is not in an uptrend.

``run_scan`` returns the full ranked lists. Cutting them to a top N is a display concern.
SPY is the regime reference and is never an idea, whatever kind the asset table gives it.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from swing_engine.config import (
    REGIME_FACTOR,
    LeaderParams,
    RiskSettings,
    SetupParams,
    UniverseFilter,
)
from swing_engine.data.assets import AssetKind
from swing_engine.indicators import Indicators, compute_indicators
from swing_engine.leaders import leader_mask, rs_percentile, rs_score
from swing_engine.panel import REFERENCE, Panel, truncate
from swing_engine.regime import Regime, breadth, market_regime
from swing_engine.setups import candidates_for
from swing_engine.sizing import Capacity, Sizing, overlap_flags, size_idea
from swing_engine.universe import eligibility

BENCHMARK = REFERENCE
"""The market-regime reference series (SPY). It must have a bar on the scan session, and its
sessions are the panel's rows (:func:`~swing_engine.panel.build_panel`)."""

RankBy = Literal["rs", "proximity"]


class ScanError(RuntimeError):
    """The scan cannot run, for example because SPY has no bar on the scan session."""


@dataclass(frozen=True)
class SetupSpec:
    """A setup to scan for: its parameters, whether it is validated, whether the regime gates it.

    ``live`` is True only for a setup with a passing out-of-sample verdict; everything else is
    shown as unproven. ``regime_gate=False`` is for a setup whose regime filter did not help
    out of sample (see the module docstring).
    """

    params: SetupParams
    live: bool
    regime_gate: bool = True


@dataclass(frozen=True)
class Idea:
    """One sized trade idea: buy above ``trigger`` with a protective ``stop``.

    ``overlaps`` lists open positions it has moved with lately, and ``regime_warning`` is set on
    an ungated setup's idea when the market is not in an uptrend.
    """

    symbol: str
    setup: str
    version: str
    trigger: float
    stop: float
    atr14: float
    rs_pct: float
    rank: int
    sizing: Sizing
    overlaps: list[str]
    live: bool
    regime_warning: bool


@dataclass(frozen=True)
class ScanResult:
    session: date
    regime: Regime
    regime_factor: float
    breadth: float
    universe_size: int
    ideas: list[Idea]
    watch_only: list[Idea]
    capacity: Capacity


@dataclass(frozen=True)
class ScanContext:
    """Everything a scan (or a stock page) needs, computed on data up to ``session`` only."""

    session: pd.Timestamp
    panel: Panel
    ind: Indicators
    eligible: pd.DataFrame
    rs_pct: pd.DataFrame
    leaders: pd.DataFrame
    regime: pd.Series
    kinds: Mapping[str, AssetKind]


def build_context(
    panel: Panel,
    kinds: Mapping[str, AssetKind],
    as_of: date,
    universe: UniverseFilter = UniverseFilter(),  # noqa: B008  (frozen dataclass)
) -> ScanContext:
    """Truncate ``panel`` to ``as_of``, then compute indicators, eligibility, RS, leaders, regime.

    Raises :class:`ScanError` when SPY is not in the panel or has no bar on ``as_of`` (a weekend,
    a holiday or a date outside the data): the regime is never inferred from an older bar.
    """
    session = pd.Timestamp(as_of)
    cut = truncate(panel, session)
    _require_benchmark(cut, session)

    ind = compute_indicators(cut)
    eligible = eligibility(cut, kinds, universe)
    eligible[BENCHMARK] = False
    rs_pct = rs_percentile(rs_score(cut.close), eligible)
    leaders = leader_mask(cut.close, ind, rs_pct, eligible, LeaderParams())
    regime = market_regime(cut.close[BENCHMARK])
    return ScanContext(
        session=session,
        panel=cut,
        ind=ind,
        eligible=eligible,
        rs_pct=rs_pct,
        leaders=leaders,
        regime=regime,
        kinds=kinds,
    )


def _require_benchmark(panel: Panel, session: pd.Timestamp) -> None:
    day = f"{session:%Y-%m-%d}"
    if BENCHMARK not in panel.close.columns:
        raise ScanError(f"{BENCHMARK} is not in the data: the market regime can't be read ({day})")
    if session not in panel.close.index or pd.isna(panel.close.at[session, BENCHMARK]):
        raise ScanError(
            f"no {BENCHMARK} bar on {day}: the market regime can't be read, so nothing was scanned"
        )


@dataclass(frozen=True)
class _Pending:
    """An idea waiting for its rank: ``key`` sorts best-first (higher is better)."""

    key: float
    idea: Idea


def _ranked(pending: list[_Pending], flags: Mapping[str, list[str]]) -> list[Idea]:
    """Order best-first (key desc, then symbol, setup and version), number from 1, add overlaps."""
    pending = sorted(pending, key=lambda p: (-p.key, p.idea.symbol, p.idea.setup, p.idea.version))
    return [
        dataclasses.replace(p.idea, rank=n, overlaps=list(flags.get(p.idea.symbol, [])))
        for n, p in enumerate(pending, start=1)
    ]


def run_scan(
    panel: Panel,
    kinds: Mapping[str, AssetKind],
    as_of: date,
    settings: RiskSettings,
    setups: Sequence[SetupSpec],
    open_positions: Sequence[tuple[str, float]] = (),
    universe: UniverseFilter = UniverseFilter(),  # noqa: B008  (frozen dataclass)
    rank_by: RankBy = "rs",
) -> ScanResult:
    """Scan the panel as of ``as_of`` and return the full ranked ideas and watch-only lists.

    ``open_positions`` holds ``(symbol, open risk dollars)`` for what is already held. Ideas from
    every spec are ranked together: ``rank_by="rs"`` puts the highest relative-strength percentile
    first, ``"proximity"`` puts the idea whose close is nearest its trigger (``close / trigger``
    highest) first; ties go to the symbol, then the setup. A stock that is a candidate for two
    specs appears once per spec.
    """
    if rank_by not in ("rs", "proximity"):
        raise ValueError(f"rank_by must be 'rs' or 'proximity', not {rank_by!r}")
    ctx = build_context(panel, kinds, as_of, universe)
    session = ctx.session
    regime = Regime(ctx.regime.loc[session])
    market_factor = REGIME_FACTOR[regime.value]
    close = ctx.panel.close.loc[session]

    active: list[_Pending] = []
    parked: list[_Pending] = []
    for spec in setups:
        factor = market_factor if spec.regime_gate else 1.0
        warn = not spec.regime_gate and regime is not Regime.UPTREND
        found = candidates_for(ctx.panel, ctx.ind, ctx.leaders, ctx.rs_pct, spec.params)
        for row in found[found["date"] == session].itertuples(index=False):
            trigger, stop = float(row.trigger), float(row.stop)
            idea = Idea(
                symbol=str(row.symbol),
                setup=str(row.setup),
                version=str(row.version),
                trigger=trigger,
                stop=stop,
                atr14=float(row.atr14),
                rs_pct=float(row.rs_pct),
                rank=0,
                sizing=size_idea(trigger, stop, settings, factor),
                overlaps=[],
                live=spec.live,
                regime_warning=warn,
            )
            key = idea.rs_pct if rank_by == "rs" else float(close[idea.symbol]) / trigger
            (parked if factor <= 0.0 else active).append(_Pending(key, idea))

    symbols = [p.idea.symbol for p in (*active, *parked)]
    flags = overlap_flags(
        ctx.panel.close,
        session,
        symbols,
        [symbol for symbol, _ in open_positions],
        settings.overlap_window,
        settings.overlap_corr,
    )
    return ScanResult(
        session=session.date(),
        regime=regime,
        regime_factor=market_factor,
        breadth=float(breadth(ctx.panel.close, ctx.ind.sma50, ctx.eligible).loc[session]),
        universe_size=int(ctx.eligible.loc[session].sum()),
        ideas=_ranked(active, flags),
        watch_only=_ranked(parked, flags),
        capacity=Capacity(
            open_positions=len(open_positions),
            max_positions=settings.max_positions,
            open_risk_dollars=float(sum(risk for _, risk in open_positions)),
            max_risk_dollars=settings.equity * settings.max_total_risk_pct,
        ),
    )


def default_setups(repo: Path | None = None) -> list[SetupSpec]:
    """Both setup families (breakout, then pullback) as their committed validation left them.

    Each family runs with the parameters pre-registered for its latest out-of-sample version
    (its defaults before the first run, or when that file is missing or edited), is Live only
    when that run passed the gate and its pre-registration is unchanged since, and keeps the
    regime gate unless that run found the regime filter did not help. ``repo`` is where
    ``ml/validation/`` lives; None means the current directory. Nothing is re-run here, and a
    bad file never raises: it makes its family Research only.
    """
    # Imported here: the validation harness itself builds on this module.
    from swing_engine.validation.run import latest_report, load_verdicts, params_for

    root = Path.cwd() if repo is None else repo
    specs = []
    for family, verdict in load_verdicts(root).items():
        report = latest_report(root, family)
        specs.append(
            SetupSpec(
                params_for(family, verdict["version"], root),
                live=verdict["live"],
                regime_gate=True if report is None else bool(report["regime_filter"]["helps"]),
            )
        )
    return specs


def json_safe(value: Any, *, infinity_as_str: bool = False) -> Any:
    """Plain JSON types: ISO dates, enum values, Python numbers, NaN and infinities as None.

    With ``infinity_as_str`` an infinity is kept as the string ``"inf"`` or ``"-inf"`` (which
    ``float()`` reads back) instead of None; NaN is None either way.
    """
    if isinstance(value, Enum):
        return json_safe(value.value, infinity_as_str=infinity_as_str)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        if math.isfinite(value):
            return float(value)
        if infinity_as_str and math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): json_safe(v, infinity_as_str=infinity_as_str) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v, infinity_as_str=infinity_as_str) for v in value]
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


def scan_to_dict(r: ScanResult) -> dict[str, Any]:
    """The scan as a JSON-safe dict (``json.dumps`` works, NaN becomes None)."""
    safe: dict[str, Any] = json_safe(dataclasses.asdict(r))
    return safe
