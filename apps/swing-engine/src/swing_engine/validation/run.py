"""The validation runs: the in-sample grid, the one-shot out-of-sample test, and the verdicts.

Files live under ``ml/validation/<family>/`` in the repo and are committed:

* ``<version>.insample.json``: every grid variant's in-sample stats and the chosen one;
* ``<version>.prereg.json``: the pre-registration (see :mod:`~swing_engine.validation.prereg`);
* ``<version>.report.json``: the out-of-sample report and its verdict.

A version is ``<family>-v<N>``, where N is its run index: one more than the family's existing
reports. JSON infinities are kept as the strings ``"inf"``/``"-inf"`` and NaN is null, so every
stored number reads back with ``float()``.

**No look-ahead.** :func:`run_insample` builds one market context cut at the last SPY session on
or before ``IS_END`` before computing anything, and every grid variant reads only that context,
so nothing from 2023 on reaches a variant's stats. A trade still open at that cut stays open and
is left out of the stats, never closed early. The in-sample simulation ignores the asset table's
delisted flags too: they say what is true today, not on ``IS_END``, and the flag would close a
trade that was open at the cut. A delisting that happened inside the window still ends its trade
at the segment break, as the bars alone show. :func:`run_oos` builds its context cut at the
freeze (the last SPY session in the data) and dates its candidates from the out-of-sample start.
No context depends on a setup's parameters, so each window's context is built once.

**What the out-of-sample run checks first.** The budget, the run index, the committed and sealed
pre-registration, and that its parameters are exactly the in-sample choice on the same data (the
grid is recomputed), so the variant that runs is the one the trial count accounts for.

**What it reports** (spec §7.8). Trade-level stats use every triggered idea in a non-downtrend
regime, uncapped (``oos_trades``), and are what the gate reads, together with the random-entry
baseline on the same candidates, the setup-alone portfolio's drawdown against SPY's buy-and-hold,
and the delisted-data coverage. The regime filter and the RS ranking are judged as well (see
:func:`regime_filter_helps` and :func:`rs_ranking`). The deflated Sharpe uses the setup-alone
portfolio's daily (not annualised) Sharpe, and counts every grid variant plus every
out-of-sample run so far as a trial.

**Reading verdicts back.** :func:`load_verdicts`, :func:`params_for` and :func:`ranking_choice`
serve the nightly scan; they never run anything and never raise on a bad file. A family is Live
only when its latest report passed and its pre-registration is still the sealed file that run
used (same sha256). An unreadable report makes the family Research only.
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
from swing_engine.backtest.trades import context_candidates, simulate_candidates
from swing_engine.config import ExecutionParams, RiskSettings, SetupParams
from swing_engine.data.assets import AssetKind
from swing_engine.panel import Panel, build_panel
from swing_engine.scan import BENCHMARK, RankBy, ScanContext, build_context, json_safe
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
PREREG_CHANGED = "pre-registration missing or changed since its run"
REPORT_UNREADABLE = "report unreadable"
_EX = ExecutionParams()


class BudgetError(RuntimeError):
    """The family's out-of-sample budget is spent, or this version already ran out of sample."""


class ReportError(ValueError):
    """A report file under ``ml/validation/`` cannot be read or lacks a field the protocol uses."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"unreadable out-of-sample report {path}: {reason}")
        self.path = path


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


def _version_ok(family: str, version: object) -> bool:
    return isinstance(version, str) and bool(re.fullmatch(rf"{family}-v[1-9][0-9]*", version))


def _validation_file(repo: Path, family: str, version: str, kind: str) -> Path:
    """``ml/validation/<family>/<version>.<kind>.json``; the version must be ``<family>-v<N>``."""
    if not _version_ok(check_family(family), version):
        raise ValueError(f"version must look like {family}-v1, {family}-v2, ..., not {version!r}")
    return _family_dir(repo, family) / f"{version}.{kind}.json"


def _jsonable(value: Any) -> Any:
    """``value`` as stored: plain JSON with infinities as ``"inf"``/``"-inf"`` and NaN as null."""
    return json_safe(value, infinity_as_str=True)


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _read_report(path: Path, family: str) -> dict[str, Any]:
    """The report at ``path`` with the fields the protocol reads checked; else :class:`ReportError`."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReportError(path, f"not readable JSON ({exc})") from None
    try:
        checks = {
            "run_index": type(doc["run_index"]) is int and doc["run_index"] >= 1,
            "version": _version_ok(family, doc["version"]),
            "freeze_date": isinstance(doc["freeze_date"], str),
            "verdict.passed": isinstance(doc["verdict"]["passed"], bool),
            "verdict.failures": isinstance(doc["verdict"]["failures"], list),
            "regime_filter.helps": isinstance(doc["regime_filter"]["helps"], bool),
            "ranking.rs_helps": isinstance(doc["ranking"]["rs_helps"], bool),
        }
        date.fromisoformat(doc["freeze_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportError(path, f"a required field is missing or malformed ({exc!r})") from None
    bad = [name for name, ok in checks.items() if not ok]
    if bad:
        raise ReportError(path, f"malformed {', '.join(bad)}")
    report: dict[str, Any] = doc
    return report


def _reports(repo: Path, family: str) -> list[tuple[Path, dict[str, Any]]]:
    """The family's out-of-sample reports with their paths, oldest run first.

    Strict: raises :class:`ReportError` (a ``ValueError``) naming the first unreadable file.
    """
    found = [
        (path, _read_report(path, family))
        for path in sorted(_family_dir(repo, family).glob("*.report.json"))
    ]
    return sorted(found, key=lambda pair: int(pair[1]["run_index"]))


def latest_report(repo: Path, family: str) -> dict[str, Any] | None:
    """The family's report with the highest ``run_index``; None before its first run, and also
    when any of its reports cannot be read (never raises)."""
    try:
        reports = _reports(repo, family)
    except ReportError:
        return None
    return reports[-1][1] if reports else None


def _sealed_prereg(repo: Path, family: str, version: str) -> Prereg | None:
    """The pre-registration for ``version`` when it exists, reads and is still sealed; else None."""
    try:
        prereg = load_prereg(_validation_file(repo, family, version, "prereg"))
    except PreregError:
        return None
    return prereg if prereg.is_sealed() else None


def load_verdicts(repo: Path) -> dict[str, dict[str, Any]]:
    """Per family, in scan order: the latest report's version, Live or not, and its failures.

    Live needs the latest report to have passed AND its pre-registration to be the sealed file
    that run recorded (``prereg_sha256``); otherwise the failures say why. A family with an
    unreadable report is not Live. Never raises on a bad file.
    """
    verdicts: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        try:
            reports = _reports(repo, family)
        except ReportError as exc:
            log.warning("validation.report_unreadable", family=family, error=str(exc))
            verdicts[family] = {
                "version": None,
                "live": False,
                "failures": [REPORT_UNREADABLE],
                "report_path": exc.path.relative_to(repo).as_posix(),
            }
            continue
        if not reports:
            verdicts[family] = {
                "live": False,
                "version": None,
                "failures": ["not yet tested"],
                "report_path": None,
            }
            continue
        path, report = reports[-1]
        live, failures = bool(report["verdict"]["passed"]), list(report["verdict"]["failures"])
        if live:
            prereg = _sealed_prereg(repo, family, report["version"])
            if prereg is None or prereg.sha256 != report.get("prereg_sha256"):
                live, failures = False, [PREREG_CHANGED]
        verdicts[family] = {
            "version": report["version"],
            "live": live,
            "failures": failures,
            "report_path": path.relative_to(repo).as_posix(),
        }
    return verdicts


def _setup_params(family: str, fields: Mapping[str, Any]) -> SetupParams:
    params: SetupParams = type(DEFAULT_PARAMS[family])(**fields)
    return params


def params_for(family: str, version: str | None, repo: Path) -> SetupParams:
    """The parameters pre-registered for ``version``, or the family's defaults when there is no
    sealed, readable pre-registration for it (so research-only ideas keep flowing)."""
    default = DEFAULT_PARAMS[check_family(family)]
    if version is None:
        return default
    prereg = _sealed_prereg(repo, family, version)
    if prereg is None:
        return default
    try:
        return _setup_params(family, prereg.params)
    except TypeError:
        return default


def ranking_choice(repo: Path) -> RankBy:
    """``"proximity"`` when a Live family's report found that RS ranking did not help, else ``"rs"``."""
    verdicts = load_verdicts(repo)
    for family in FAMILIES:
        report = latest_report(repo, family) if verdicts[family]["live"] else None
        if report is not None and not report["ranking"]["rs_helps"]:
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
    again only ``REFRESH_MONTHS`` after its latest freeze, on the data since that freeze. An
    unreadable report raises :class:`ReportError` (a ``ValueError``) naming the file: the runs
    can't be counted without it.
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


def _last_spy_session(panel: Panel, on_or_before: date | None = None) -> date:
    if BENCHMARK not in panel.close.columns:
        raise ValueError(f"{BENCHMARK} is not in the data: run `python -m swing_engine backfill`")
    sessions = panel.close[BENCHMARK].dropna().index
    if on_or_before is not None:
        sessions = sessions[sessions <= pd.Timestamp(on_or_before)]
    if sessions.empty:
        raise ValueError(f"no {BENCHMARK} session on or before {on_or_before} in the data")
    last: date = pd.Timestamp(sessions[-1]).date()
    return last


def _in_sample(data: ValidationData) -> ScanContext:
    """The one in-sample context: the panel cut at the last SPY session on or before IS_END.

    ``build_context`` truncates before it computes anything, so nothing after IS_END is read.
    """
    return build_context(data.panel, data.kinds, _last_spy_session(data.panel, IS_END))


def _is_stats(ctx: ScanContext, params: SetupParams) -> dict[str, Any]:
    """Trade stats of ``params`` in sample: non-downtrend trades closed by the cut."""
    candidates = context_candidates(ctx, params, IS_START)
    # No delisted flags in sample: see the module docstring.
    trades = simulate_candidates(ctx.panel, ctx.ind, candidates, params, _EX, frozenset())
    return trade_stats(regime_on(trades))


def _grid(ctx: ScanContext, family: str) -> list[dict[str, Any]]:
    """One row per grid variant, in grid order: ``{"params": <the combo>, **trade stats}``."""
    rows = []
    for combo in grid_variants(family):
        overrides: dict[str, Any] = dict(combo)
        params = dataclasses.replace(DEFAULT_PARAMS[family], **overrides)
        rows.append({"params": combo, **_is_stats(ctx, params)})
        log.info("validation.variant", family=family, params=combo, n=rows[-1]["n"])
    return rows


def _chosen_params(family: str, chosen: Mapping[str, Any], version: str) -> SetupParams:
    """The family's defaults with the chosen grid row's values, labelled ``version``."""
    overrides: dict[str, Any] = dict(chosen["params"])
    return dataclasses.replace(DEFAULT_PARAMS[family], **overrides, version=version)


def run_insample(
    family: str, repo: Path, data: ValidationData | None = None, hypothesis: str = ""
) -> dict[str, Any]:
    """Run the in-sample grid, choose a variant, and write its draft pre-registration.

    Writes ``<version>.insample.json`` (every grid row and the chosen one) and a draft
    ``<version>.prereg.json`` holding the chosen parameters, the gate, the windows and
    ``hypothesis`` (required for a revision). Returns a summary with both paths. ``data`` is
    loaded from the store when not given. Raises ``ValueError`` when no variant has enough trades
    or a report already on file cannot be read.
    """
    check_family(family)
    reports = _reports(repo, family)
    version = f"{family}-v{len(reports) + 1}"
    if data is None:
        data = asyncio.run(load_validation_data())

    rows = _grid(_in_sample(data), family)
    chosen = select_variant(rows)
    params = _chosen_params(family, chosen, version)
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    is_window = [IS_START.isoformat(), IS_END.isoformat()]

    insample_path = _validation_file(repo, family, version, "insample")
    record = {
        "family": family,
        "version": version,
        "is_window": is_window,
        "n_variants": len(rows),
        "grid": _jsonable(rows),
        "chosen": _jsonable(chosen),
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


def _check_prereg_matches_run(prereg: Prereg, family: str, version: str, oos_start: date) -> None:
    """The pre-registration is this run's and declares the protocol's gate and windows."""
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


def regime_filter_helps(
    expectancy_on: float, expectancy_off: float, max_dd_on: float, max_dd_off: float
) -> bool:
    """Whether the regime filter earned its place out of sample.

    False when trading every regime had the higher expectancy (``expectancy_off >
    expectancy_on``) or the setup-alone portfolio with the filter had the deeper drawdown
    (``max_dd_on < max_dd_off``, both NEGATIVE). An expectancy that cannot be measured (NaN)
    keeps the filter, as far as expectancy goes.
    """
    expectancy_ok = (
        math.isnan(expectancy_on) or math.isnan(expectancy_off) or expectancy_on >= expectancy_off
    )
    return bool(expectancy_ok and max_dd_on >= max_dd_off)


def rs_ranking(trades: pd.DataFrame) -> dict[str, Any]:
    """Expectancy by RS quintile (weakest first) of the closed trades, and whether RS helps.

    Quintiles are ``pd.qcut(rs_pct, 5, duplicates="drop")``. ``rs_helps`` is the top group's
    expectancy above the bottom group's, and True when that cannot be measured (fewer than two
    groups, or a NaN end).
    """
    closed = trades.loc[trades["status"] == "closed"]
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

    Before any data is read it refuses with :class:`BudgetError` when the version already has a
    report or the family's budget is spent, with ``ValueError`` when ``version`` is not the
    next run (``<family>-v<run_index>``) or a report on file is unreadable, and with
    :class:`~swing_engine.validation.prereg.PreregError` when the pre-registration is not
    committed, unchanged and sealed, or does not match this run. Once the in-sample grid is
    recomputed it also refuses a pre-registration whose parameters are not the in-sample choice.
    The report (``<version>.report.json``) is written whatever the verdict, and returned as
    written.
    """
    check_family(family)
    report_path = _validation_file(repo, family, version, "report")
    if report_path.exists():
        raise BudgetError(
            f"{version} already has an out-of-sample report ({report_path.name}): each version "
            "runs out of sample once"
        )
    run_index, oos_start = check_budget(repo, family, now)
    if version != f"{family}-v{run_index}":
        raise ValueError(
            f"the next out-of-sample run of {family} is {family}-v{run_index}, not {version}"
        )
    prereg = check_committed(_validation_file(repo, family, version, "prereg"), repo)
    _check_prereg_matches_run(prereg, family, version, oos_start)
    if data is None:
        data = asyncio.run(load_validation_data())

    grid = _grid(_in_sample(data), family)
    chosen = select_variant(grid)
    params = _chosen_params(family, chosen, version)
    if prereg.params != dataclasses.asdict(params):
        raise PreregError(f"{version} is not the in-sample choice; re-run insample")
    is_stats = {k: v for k, v in chosen.items() if k != "params"}

    freeze = _last_spy_session(data.panel)
    if freeze < oos_start:
        raise ValueError(f"no out-of-sample data: the last {BENCHMARK} session is {freeze}")
    ctx = build_context(data.panel, data.kinds, freeze)  # cut at the freeze before any work

    def oos_trades(p: SetupParams) -> tuple[pd.DataFrame, pd.DataFrame]:
        candidates = context_candidates(ctx, p, oos_start)
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

    report = _jsonable(
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
                "helps": regime_filter_helps(
                    on_stats["expectancy_r"],
                    off_stats["expectancy_r"],
                    alone.max_dd,
                    ungated.max_dd,
                ),
                "on": on_stats,
                "off": off_stats,
                "max_dd": {"on": alone.max_dd, "off": ungated.max_dd},
            },
            "ranking": rs_ranking(on),
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
