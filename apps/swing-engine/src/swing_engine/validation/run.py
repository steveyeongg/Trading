"""The validation runs: the in-sample grid, the one-shot out-of-sample test, and the verdicts.

Files live under ``ml/validation/<family>/`` in the repo and are committed:

* ``<version>.insample.json``: every grid variant's in-sample stats and the chosen one;
* ``<version>.prereg.json``: the pre-registration (see :mod:`~swing_engine.validation.prereg`);
* ``<version>.report.json``: the out-of-sample report and its verdict.

A version is ``<family>-v<N>`` with N one more than the family's existing reports.

**No look-ahead.** :func:`run_insample` cuts the panel at the last SPY session on or before
``IS_END`` before computing anything, so nothing from 2023 on reaches a variant's stats. A trade
still open at that cut stays open and is left out of the stats, never closed early. The in-sample
simulation ignores the asset table's delisted flags too: they say what is true today, not on
``IS_END``, and the flag would close a trade that was open at the cut. A delisting that happened
inside the window still ends its trade at the segment break, as the bars alone show.
:func:`run_oos` cuts the panel at the freeze (the last SPY session in the data), dates its
candidates from the out-of-sample start, and computes indicators and leaders on that same cut.

**What the out-of-sample run reports** (spec §7.8). Trade-level stats use every triggered idea in
a non-downtrend regime, uncapped (``oos_trades``), and are what the gate reads, together with the
random-entry baseline on the same candidates, the setup-alone portfolio's drawdown against SPY's
buy-and-hold, and the delisted-data coverage. The regime filter and the RS ranking are judged as
well: ``regime_filter.helps`` is False when trading every regime had the higher expectancy or the
shallower portfolio drawdown, and ``ranking.rs_helps`` is False when the top RS quintile did not
beat the bottom one. When either cannot be measured (no trades, one quintile) the default is kept.
The deflated Sharpe uses the setup-alone portfolio's daily (not annualised) Sharpe, and counts
every grid variant plus every out-of-sample run so far as a trial.

:func:`load_verdicts`, :func:`params_for` and :func:`ranking_choice` read the committed files
back for the nightly scan; they never run anything.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from atlas_shared.logging import get_logger
from backtest_service.metrics import deflated_sharpe, sharpe

from swing_engine import store
from swing_engine.backtest.baseline import random_entry_baseline
from swing_engine.backtest.portfolio import PortfolioResult, buy_and_hold, simulate_portfolio
from swing_engine.backtest.stats import by_year, regime_on, trade_stats
from swing_engine.backtest.trades import candidate_history, simulate_candidates
from swing_engine.config import ExecutionParams, RiskSettings, SetupParams
from swing_engine.data.assets import AssetKind
from swing_engine.indicators import Indicators, compute_indicators
from swing_engine.panel import Panel, build_panel, truncate
from swing_engine.scan import BENCHMARK, RankBy, build_context, json_safe
from swing_engine.validation.prereg import (
    Prereg,
    PreregError,
    check_committed,
    load_prereg,
    write_draft,
)
from swing_engine.validation.protocol import (
    DEFAULT_PARAMS,
    FAMILIES,
    IS_END,
    IS_START,
    MAX_OOS_RUNS,
    OOS_START,
    REFRESH_MONTHS,
    GateThresholds,
    check_family,
    evaluate_gate,
    grid_variants,
    select_variant,
)

log = get_logger("swing.validation")

VALIDATION_DIR = Path("ml") / "validation"
BASELINE_SEEDS = 100
_EX = ExecutionParams()
_NOT_TESTED: dict[str, Any] = {
    "live": False,
    "version": None,
    "failures": ["not yet tested"],
    "report_path": None,
}


class BudgetError(RuntimeError):
    """The family's out-of-sample budget is spent, or this version already ran out of sample."""


@dataclass(frozen=True)
class ValidationData:
    """Everything a validation run reads: the bars, asset kinds, delisted names and coverage.

    ``delisted`` holds the symbols whose asset status is inactive. ``coverage`` is the share of
    inactive common stocks that have any daily bar (NaN when the asset table knows none), and
    ``meta`` describes the data for :func:`data_fingerprint` (see :func:`panel_meta`).
    """

    panel: Panel
    kinds: Mapping[str, AssetKind]
    delisted: frozenset[str]
    coverage: float
    meta: dict[str, Any]


# ---------------------------------------------------------------------------- data


def panel_meta(panel: Panel) -> dict[str, Any]:
    """What a fingerprint needs: each symbol's last bar, the bar count and the date range."""
    close = panel.close
    last = close.apply(pd.Series.last_valid_index).dropna()
    present = close.notna()
    dated = close.index[present.any(axis=1).to_numpy()]
    return {
        "last_bars": {str(s): pd.Timestamp(d).date().isoformat() for s, d in sorted(last.items())},
        "rows": int(present.to_numpy().sum()),
        "first_date": dated[0].date().isoformat() if len(dated) else None,
        "last_date": dated[-1].date().isoformat() if len(dated) else None,
    }


def data_fingerprint(meta: Mapping[str, Any]) -> dict[str, Any]:
    """Symbol count, date range, row count, and the sha256 of the sorted ``symbol:last_bar`` list."""
    listing = "\n".join(f"{s}:{d}" for s, d in sorted(meta["last_bars"].items()))
    return {
        "symbols": len(meta["last_bars"]),
        "first_date": meta["first_date"],
        "last_date": meta["last_date"],
        "rows": meta["rows"],
        "sha256": hashlib.sha256(listing.encode()).hexdigest(),
    }


async def load_validation_data(min_dollar_volume: float = 20_000_000.0) -> ValidationData:
    """The stored daily bars from ``IS_START`` to today for the liquid stocks plus SPY.

    Adjusted and raw bars both load (raw only feeds the point-in-time price floor). Kinds and
    delisted names come from the asset table. Raises ``ValueError`` when no bars are stored.
    """
    symbols = sorted({*await store.liquid_prefilter(min_dollar_volume), BENCHMARK})
    today = datetime.now(UTC).date()
    bars = await store.load_daily_bars(symbols, IS_START, today)
    if bars.empty:
        raise ValueError(
            f"no daily bars stored since {IS_START}: run `python -m swing_engine backfill` first"
        )
    raw = await store.load_daily_bars(symbols, IS_START, today, resolution=store.RAW_DAILY)
    panel = build_panel(bars, raw_bars=raw)

    assets = await store.load_assets()
    symbol = assets["symbol"].astype(str)
    kinds = {s: AssetKind(k) for s, k in zip(symbol, assets["kind"], strict=True)}
    inactive = (assets["status"] == "inactive").to_numpy(dtype=bool)
    dead_common = symbol[inactive & (assets["kind"] == AssetKind.COMMON.value).to_numpy(dtype=bool)]
    with_bars = set(await store.last_bar_dates())
    coverage = float(dead_common.isin(with_bars).mean()) if len(dead_common) else math.nan
    return ValidationData(
        panel=panel,
        kinds=kinds,
        delisted=frozenset(symbol[inactive]),
        coverage=coverage,
        meta=panel_meta(panel),
    )


# ---------------------------------------------------------------------------- files


def _family_dir(repo: Path, family: str) -> Path:
    return repo / VALIDATION_DIR / check_family(family)


def _validation_file(repo: Path, family: str, version: str, kind: str) -> Path:
    """``ml/validation/<family>/<version>.<kind>.json``; the version must be ``<family>-v<N>``."""
    if not re.fullmatch(rf"{re.escape(family)}-v[1-9][0-9]*", version):
        raise ValueError(f"version must look like {family}-v1, {family}-v2, ..., not {version!r}")
    return _family_dir(repo, family) / f"{version}.{kind}.json"


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _reports(repo: Path, family: str) -> list[tuple[Path, dict[str, Any]]]:
    """The family's out-of-sample reports with their paths, oldest run first."""
    found = [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in _family_dir(repo, family).glob("*.report.json")
    ]
    return sorted(found, key=lambda pair: int(pair[1]["run_index"]))


def latest_report(repo: Path, family: str) -> dict[str, Any] | None:
    """The family's report with the highest ``run_index``, or None before its first run."""
    reports = _reports(repo, family)
    return reports[-1][1] if reports else None


def load_verdicts(repo: Path) -> dict[str, dict[str, Any]]:
    """Per family, in scan order: the latest report's version, Live or not, and its failures."""
    verdicts: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        reports = _reports(repo, family)
        if not reports:
            verdicts[family] = {**_NOT_TESTED, "failures": list(_NOT_TESTED["failures"])}
            continue
        path, report = reports[-1]
        verdicts[family] = {
            "version": report["version"],
            "live": bool(report["verdict"]["passed"]),
            "failures": list(report["verdict"]["failures"]),
            "report_path": path.relative_to(repo).as_posix(),
        }
    return verdicts


def _setup_params(family: str, fields: Mapping[str, Any]) -> SetupParams:
    params: SetupParams = type(DEFAULT_PARAMS[family])(**fields)
    return params


def params_for(family: str, version: str | None, repo: Path) -> SetupParams:
    """The parameters pre-registered for ``version``; the family's defaults without that file."""
    default = DEFAULT_PARAMS[check_family(family)]
    if version is None:
        return default
    path = _validation_file(repo, family, version, "prereg")
    if not path.exists():
        return default
    return _setup_params(family, load_prereg(path).params)


def ranking_choice(repo: Path) -> RankBy:
    """``"proximity"`` when a Live family's report found that RS ranking did not help, else ``"rs"``."""
    for family in FAMILIES:
        report = latest_report(repo, family)
        if report and report["verdict"]["passed"] and not report["ranking"]["rs_helps"]:
            return "proximity"
    return "rs"


def _next_oos_start(reports: list[tuple[Path, dict[str, Any]]]) -> date:
    """``OOS_START`` while the budget lasts; after it, the day after the latest freeze."""
    if len(reports) < MAX_OOS_RUNS:
        return OOS_START
    return date.fromisoformat(reports[-1][1]["freeze_date"]) + timedelta(days=1)


def check_budget(repo: Path, family: str, now: datetime) -> tuple[int, date]:
    """The next run's index and out-of-sample start, or :class:`BudgetError` when it may not run.

    Up to ``MAX_OOS_RUNS`` runs share the window from ``OOS_START``. After that a family runs
    again only ``REFRESH_MONTHS`` after its latest freeze, on the data since that freeze.
    """
    reports = _reports(repo, family)
    if len(reports) >= MAX_OOS_RUNS:
        freeze = date.fromisoformat(reports[-1][1]["freeze_date"])
        reopens = (pd.Timestamp(freeze) + pd.DateOffset(months=REFRESH_MONTHS)).date()
        if now.date() < reopens:
            raise BudgetError(
                f"{family}: the out-of-sample budget is spent ({len(reports)} runs, at most "
                f"{MAX_OOS_RUNS}); a fresh window opens on {reopens}, {REFRESH_MONTHS} months "
                f"after the last freeze ({freeze})"
            )
    return len(reports) + 1, _next_oos_start(reports)


# ---------------------------------------------------------------------------- in sample


@dataclass(frozen=True)
class _InSample:
    """The panel cut at the last SPY session on or before ``IS_END``, and its indicators."""

    panel: Panel
    ind: Indicators
    end: date


def _last_spy_session(panel: Panel, on_or_before: date | None = None) -> pd.Timestamp:
    if BENCHMARK not in panel.close.columns:
        raise ValueError(f"{BENCHMARK} is not in the data: run `python -m swing_engine backfill`")
    sessions = panel.close[BENCHMARK].dropna().index
    if on_or_before is not None:
        sessions = sessions[sessions <= pd.Timestamp(on_or_before)]
    if sessions.empty:
        raise ValueError(f"no {BENCHMARK} session on or before {on_or_before} in the data")
    return pd.Timestamp(sessions[-1])


def _in_sample(data: ValidationData) -> _InSample:
    cut = _last_spy_session(data.panel, IS_END)
    panel = truncate(data.panel, cut)  # first, so nothing after IS_END is ever computed on
    return _InSample(panel=panel, ind=compute_indicators(panel), end=cut.date())


def _is_stats(
    window: _InSample, kinds: Mapping[str, AssetKind], params: SetupParams
) -> dict[str, Any]:
    """Trade stats of ``params`` in sample: non-downtrend trades closed by the cut."""
    candidates = candidate_history(window.panel, kinds, params, IS_START, window.end)
    # No delisted flags in sample: see the module docstring.
    trades = simulate_candidates(window.panel, window.ind, candidates, params, _EX, frozenset())
    return trade_stats(regime_on(trades))


def _grid(window: _InSample, kinds: Mapping[str, AssetKind], family: str) -> list[dict[str, Any]]:
    """One row per grid variant, in grid order: ``{"params": <the combo>, **trade stats}``."""
    rows = []
    for combo in grid_variants(family):
        overrides: dict[str, Any] = dict(combo)
        params = dataclasses.replace(DEFAULT_PARAMS[family], **overrides)
        rows.append({"params": combo, **_is_stats(window, kinds, params)})
        log.info("validation.variant", family=family, params=combo, n=rows[-1]["n"])
    return rows


def run_insample(
    family: str, repo: Path, data: ValidationData | None = None, hypothesis: str = ""
) -> dict[str, Any]:
    """Run the in-sample grid, choose a variant, and write its draft pre-registration.

    Writes ``<version>.insample.json`` (every grid row and the chosen one) and a draft
    ``<version>.prereg.json`` holding the chosen parameters, the gate, the windows and
    ``hypothesis`` (required for a revision). Returns a summary with both paths. ``data`` is
    loaded from the store when not given. Raises ``ValueError`` when no variant has enough trades.
    """
    check_family(family)
    reports = _reports(repo, family)
    version = f"{family}-v{len(reports) + 1}"
    if data is None:
        data = asyncio.run(load_validation_data())

    rows = _grid(_in_sample(data), data.kinds, family)
    chosen = select_variant(rows)
    params = dataclasses.replace(DEFAULT_PARAMS[family], **chosen["params"], version=version)
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    is_window = [IS_START.isoformat(), IS_END.isoformat()]

    insample_path = _validation_file(repo, family, version, "insample")
    grid: list[dict[str, Any]] = json_safe(rows)
    record = {
        "family": family,
        "version": version,
        "is_window": is_window,
        "n_variants": len(rows),
        "grid": grid,
        "chosen": json_safe(chosen),
        "created_at": created_at,
    }
    _write_json(insample_path, record)
    prereg_path = _validation_file(repo, family, version, "prereg")
    prereg = Prereg.create(
        family=family,
        version=version,
        params=dataclasses.asdict(params),
        gate=dataclasses.asdict(GateThresholds()),
        is_window=is_window,
        oos_start=_next_oos_start(reports).isoformat(),
        hypothesis=hypothesis,
        created_at=created_at,
    )
    write_draft(prereg_path, prereg)
    return {
        **record,
        "params": prereg.params,
        "insample_path": str(insample_path),
        "prereg_path": str(prereg_path),
    }


# ---------------------------------------------------------------------------- out of sample


def _checked_params(prereg: Prereg, family: str, version: str, oos_start: date) -> SetupParams:
    """The pre-registered parameters, after checking the file matches this run and the protocol."""
    where = f"{version}.prereg.json"
    if (prereg.family, prereg.version) != (family, version):
        raise PreregError(f"{where} is for {prereg.version} of {prereg.family}, not {version}")
    if prereg.gate != dataclasses.asdict(GateThresholds()):
        raise PreregError(f"{where} declares gate thresholds that are not the protocol's")
    if prereg.is_window != [IS_START.isoformat(), IS_END.isoformat()]:
        raise PreregError(f"{where} declares the in-sample window {prereg.is_window}")
    if prereg.oos_start != oos_start.isoformat():
        raise PreregError(
            f"{where} declares an out-of-sample start of {prereg.oos_start}, but this run's "
            f"window starts on {oos_start}"
        )
    try:
        params = _setup_params(family, prereg.params)
    except TypeError as exc:
        raise PreregError(f"{where} has parameters this engine does not know: {exc}") from None
    if params.version != version:
        raise PreregError(f"{where} parameters are labelled {params.version}, not {version}")
    return params


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Row-wise concatenation that skips empty frames (pandas warns about those)."""
    kept = [f for f in frames if len(f)] or frames[:1]
    return pd.concat(kept, ignore_index=True)


def _portfolio(result: PortfolioResult) -> dict[str, Any]:
    """The account's metrics and its equity curve sampled weekly (last value of each W-FRI)."""
    weekly = result.equity.resample("W-FRI").last().dropna()
    return {
        "cagr": result.cagr,
        "sharpe": result.sharpe,
        "max_dd": result.max_dd,
        "exposure": result.exposure,
        "trades": len(result.taken),
        "equity_weekly": [[ts.date().isoformat(), float(v)] for ts, v in weekly.items()],
    }


def _baseline(seeds: npt.NDArray[np.float64]) -> dict[str, float]:
    finite = seeds[~np.isnan(seeds)]
    if not finite.size:
        return {"median": math.nan, "p5": math.nan, "p95": math.nan}
    p5, median, p95 = np.percentile(finite, [5, 50, 95])
    return {"median": float(median), "p5": float(p5), "p95": float(p95)}


def _ranking(on: pd.DataFrame) -> dict[str, Any]:
    """Expectancy by RS quintile (weakest first) of the closed trades, and whether RS helps."""
    closed = on.loc[on["status"] == "closed"]
    quintiles: list[float] = []
    if len(closed):
        groups = pd.qcut(closed["rs_pct"], 5, duplicates="drop")
        means = closed.groupby(groups, observed=True)["r_multiple"].mean()
        quintiles = [float(x) for x in means]
    rs_helps = (
        len(quintiles) < 2
        or math.isnan(quintiles[0])
        or math.isnan(quintiles[-1])
        or quintiles[-1] > quintiles[0]
    )
    return {"quintile_expectancy": quintiles, "rs_helps": bool(rs_helps)}


def run_oos(
    family: str, version: str, repo: Path, now: datetime, data: ValidationData | None = None
) -> dict[str, Any]:
    """The one out-of-sample run of ``version``: check, freeze, compute, write the report.

    Refuses with :class:`BudgetError` when the version already has a report or the family's
    budget is spent, and with :class:`~swing_engine.validation.prereg.PreregError` when the
    pre-registration is not committed and unchanged or does not match this run. Both checks
    come before any data is read. The report (``<version>.report.json``) is written whatever
    the verdict, and returned as written.
    """
    check_family(family)
    report_path = _validation_file(repo, family, version, "report")
    if report_path.exists():
        raise BudgetError(
            f"{version} already has an out-of-sample report ({report_path.name}): each version "
            "runs out of sample once"
        )
    run_index, oos_start = check_budget(repo, family, now)
    prereg = check_committed(_validation_file(repo, family, version, "prereg"), repo)
    params = _checked_params(prereg, family, version, oos_start)
    if data is None:
        data = asyncio.run(load_validation_data())

    window = _in_sample(data)
    grid = _grid(window, data.kinds, family)
    is_stats = _is_stats(window, data.kinds, params)

    freeze = _last_spy_session(data.panel).date()
    if freeze < oos_start:
        raise ValueError(f"no out-of-sample data: the last {BENCHMARK} session is {freeze}")
    ctx = build_context(data.panel, data.kinds, freeze)  # cut at the freeze before any work

    def oos_trades(p: SetupParams) -> tuple[pd.DataFrame, pd.DataFrame]:
        candidates = candidate_history(ctx.panel, data.kinds, p, oos_start, freeze)
        return candidates, simulate_candidates(
            ctx.panel, ctx.ind, candidates, p, _EX, data.delisted
        )

    candidates, trades = oos_trades(params)
    other = next(f for f in FAMILIES if f != family)
    _, other_trades = oos_trades(params_for(other, load_verdicts(repo)[other]["version"], repo))
    on = regime_on(trades)
    on_stats, off_stats = trade_stats(on), trade_stats(trades)

    settings, close = RiskSettings(), ctx.panel.close
    alone = simulate_portfolio(trades, close, settings, oos_start, freeze)
    ungated = simulate_portfolio(
        trades.assign(regime="uptrend"), close, settings, oos_start, freeze
    )
    combined = simulate_portfolio(
        _concat([trades, other_trades]), close, settings, oos_start, freeze
    )
    spy = buy_and_hold(close[BENCHMARK], oos_start, freeze)
    baseline = _baseline(
        random_entry_baseline(
            ctx.panel,
            ctx.ind,
            ctx.leaders,
            regime_on(candidates),
            params,
            _EX,
            BASELINE_SEEDS,
            data.delisted,
        )
    )

    x_on, x_off = on_stats["expectancy_r"], off_stats["expectancy_r"]
    expectancy_ok = math.isnan(x_on) or math.isnan(x_off) or x_on >= x_off
    equity = alone.equity.to_numpy(dtype="float64")
    returns = equity[1:] / equity[:-1] - 1.0
    daily_sharpe = sharpe(returns, periods_per_year=1)
    n_trials = len(grid) + run_index
    verdict = evaluate_gate(
        on_stats,
        baseline["median"],
        alone.max_dd,
        spy.max_dd,
        data.coverage,
        GateThresholds(**prereg.gate),
    )

    report = json_safe(
        {
            "family": family,
            "version": version,
            "prereg_sha256": prereg.sha256,
            "run_index": run_index,
            "n_trials": n_trials,
            "freeze_date": freeze,
            "oos_window": [oos_start, freeze],
            "is_stats": is_stats,
            "grid": grid,
            "oos_trades": on_stats,
            "oos_trades_regime_off": off_stats,
            "by_year": by_year(on),
            "baseline": baseline,
            "portfolio_alone": _portfolio(alone),
            "portfolio_combined": _portfolio(combined),
            "spy": _portfolio(spy),
            "regime_filter": {
                "helps": bool(expectancy_ok and alone.max_dd >= ungated.max_dd),
                "on": on_stats,
                "off": off_stats,
            },
            "ranking": _ranking(on),
            "coverage": data.coverage,
            "fingerprint": data_fingerprint(data.meta),
            "deflated_sharpe": {
                "value": deflated_sharpe(daily_sharpe, n_trials, int(returns.size)),
                "observed_sharpe": daily_sharpe,
                "n_obs": int(returns.size),
                "n_trials": n_trials,
            },
            "verdict": {"passed": verdict.passed, "failures": verdict.failures},
        }
    )
    _write_json(report_path, report)
    log.info("validation.oos_done", family=family, version=version, passed=verdict.passed)
    out: dict[str, Any] = report
    return out
