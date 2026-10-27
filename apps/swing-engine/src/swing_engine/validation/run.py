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
pre-registration, that its protocol block is the rules the code has now, that a revision does
not repeat an earlier version's rules, that the last session is fully loaded (the freeze), and
that its parameters are exactly the in-sample choice on the same data (the grid is recomputed),
so the variant that runs is the one the trial count accounts for.

**What it reports** (spec §7.8). Trade-level stats use every triggered idea in a non-downtrend
regime, uncapped (``oos_trades``), and are what the gate reads: expectancy and profit factor on
all of them, the trade floor on the distinct ones (``n_distinct``; ``oos_trades_distinct`` has
the stats of those alone), together with the random-entry baseline on the same candidates, the
setup-alone portfolio's drawdown against SPY's buy-and-hold, and the delisted-data coverage. The
regime filter and the RS ranking are judged as well (see :func:`regime_filter_helps` and
:func:`rs_ranking`). The combined portfolio takes the other family only once it has had its own
out-of-sample run, with that run's parameters (:func:`other_family_params`), so one family's
report never looks at the other's window first. The deflated Sharpe uses the setup-alone
portfolio's daily (not annualised) Sharpe with the sample skew and kurtosis of its daily
returns, and counts every grid variant plus every out-of-sample run so far as a trial. The
report records the code it ran on (``code_commit``, ``code_dirty``).

**Reading verdicts back.** :func:`load_verdicts`, :func:`params_for` and :func:`ranking_choice`
serve the nightly scan; they never run anything and never raise on a bad file. A family is Live
only when its latest report passed, its pre-registration is still the sealed file that run used
(same sha256), and the rules that file sealed are the code's rules now. An unreadable report
makes the family Research only.
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
from scipy import stats as sps

from swing_engine import store
from swing_engine.backtest.baseline import random_entry_baseline
from swing_engine.backtest.portfolio import PortfolioResult, buy_and_hold, simulate_portfolio
from swing_engine.backtest.stats import by_year, distinct_trades, regime_on, trade_stats
from swing_engine.backtest.trades import context_candidates, simulate_candidates
from swing_engine.config import ExecutionParams, RiskSettings, SetupParams, UniverseFilter
from swing_engine.data.assets import AssetKind
from swing_engine.panel import Panel, build_panel, concat_panels
from swing_engine.scan import BENCHMARK, RankBy, ScanContext, build_context, json_safe
from swing_engine.validation.prereg import (
    Prereg,
    PreregError,
    check_committed,
    code_state,
    load_prereg,
    write_draft,
)
from swing_engine.validation.protocol import (
    DEFAULT_PARAMS,
    FAMILIES,
    FREEZE_COVERAGE,
    IS_END,
    IS_START,
    MAX_OOS_RUNS,
    MIN_REGIME_OFF_TRADES,
    OOS_START,
    REFRESH_MONTHS,
    GateThresholds,
    Verdict,
    check_family,
    evaluate_gate,
    grid_variants,
    protocol_block,
    select_variant,
)

log = get_logger("swing.validation")

VALIDATION_DIR = Path("ml") / "validation"
BASELINE_SEEDS = 100
LOAD_BATCH = 500
"""Symbols read and pivoted at a time by :func:`load_validation_data`."""
PREREG_CHANGED = "pre-registration missing or changed since its run"
RULES_CHANGED = "rules changed since its run"
RULES_UNSEALED = f"{RULES_CHANGED}: its pre-registration has no protocol block"
REPORT_UNREADABLE = "report unreadable"
OTHER_NOT_VALIDATED = "other family not yet validated"
OTHER_PREREG_CHANGED = "other family's pre-registration missing or changed since its run"
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


async def _load_panel(symbols: list[str], start: date, end: date) -> tuple[Panel, int, int]:
    """The panel of ``symbols`` from ``start`` to ``end``, read and pivoted ``LOAD_BATCH``
    symbols at a time, with the adjusted and raw row counts.

    Only the columns a panel uses are read (``ts, symbol, open, high, low, close, volume``
    adjusted, ``ts, symbol, close`` raw), and each batch's long rows are pivoted to wide frames
    and dropped before the next batch is read, so the peak is one batch of rows plus the panel.
    """
    batches: list[Panel] = []
    rows = raw_rows = 0
    for i in range(0, len(symbols), LOAD_BATCH):
        batch = symbols[i : i + LOAD_BATCH]
        bars = await store.load_ohlcv(batch, start, end)
        raw = await store.load_raw_closes(batch, start, end)
        rows, raw_rows = rows + len(bars), raw_rows + len(raw)
        batches.append(build_panel(bars, raw_bars=raw))
        del bars, raw
    return concat_panels(batches), rows, raw_rows


async def load_validation_data(universe: UniverseFilter | None = None) -> ValidationData:
    """The stored daily bars from ``IS_START`` to today for the liquid stocks plus SPY.

    "Liquid" is :func:`~swing_engine.store.liquid_prefilter` at ``universe``'s dollar-volume
    floor (the default :class:`~swing_engine.config.UniverseFilter`, the one the runs use): a
    necessary condition for ever being eligible, so nothing tradable is dropped. Adjusted and
    raw bars both load (raw only feeds the point-in-time price floor), in symbol batches (see
    :func:`_load_panel`), and the counts are logged. Kinds and delisted names come from the
    asset table. Raises ``ValueError`` when no bars are stored.
    """
    universe = UniverseFilter() if universe is None else universe
    liquid = await store.liquid_prefilter(universe.min_median_dollar_volume)
    symbols = sorted({*liquid, BENCHMARK})
    today = datetime.now(UTC).date()
    panel, rows, raw_rows = await _load_panel(symbols, IS_START, today)
    if not rows:
        raise ValueError(
            f"no daily bars stored since {IS_START}: run `python -m swing_engine backfill` first"
        )
    log.info(
        "validation.data_loaded",
        symbols=len(symbols),
        batches=-(-len(symbols) // LOAD_BATCH),
        rows=rows,
        raw_rows=raw_rows,
        sessions=len(panel.dates()),
    )

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


def family_reports(repo: Path, family: str) -> list[dict[str, Any]]:
    """Every out-of-sample report of ``family``, oldest run first.

    Raises :class:`ReportError` (a ``ValueError``) naming the first unreadable file.
    """
    return [report for _, report in _reports(repo, family)]


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

    Live needs the latest report to have passed, its pre-registration to be the sealed file
    that run recorded (``prereg_sha256``), AND that file's protocol block to be the rules the
    code has now (:func:`~swing_engine.validation.protocol.protocol_block`); otherwise the
    failures say why (a file with no protocol block cannot vouch for the rules). A family with
    an unreadable report is not Live. Never raises on a bad file.
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
            elif prereg.protocol is None:
                live, failures = False, [RULES_UNSEALED]
            elif prereg.protocol != protocol_block(family):
                live, failures = False, [RULES_CHANGED]
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


def _is_trades(ctx: ScanContext, params: SetupParams) -> pd.DataFrame:
    """The in-sample trades of ``params`` in a non-downtrend regime (open ones stay open)."""
    candidates = context_candidates(ctx, params, IS_START)
    # No delisted flags in sample: see the module docstring.
    trades = simulate_candidates(ctx.panel, ctx.ind, candidates, params, _EX, frozenset())
    return regime_on(trades)


def _grid(ctx: ScanContext, family: str) -> tuple[list[dict[str, Any]], list[pd.DataFrame]]:
    """One row per grid variant, in grid order: ``{"params": <the combo>, **trade stats}``, and
    each variant's in-sample trades in the same order."""
    rows: list[dict[str, Any]] = []
    trades: list[pd.DataFrame] = []
    for combo in grid_variants(family):
        overrides: dict[str, Any] = dict(combo)
        params = dataclasses.replace(DEFAULT_PARAMS[family], **overrides)
        trades.append(_is_trades(ctx, params))
        rows.append({"params": combo, **trade_stats(trades[-1])})
        log.info(
            "validation.variant",
            family=family,
            params=combo,
            n=rows[-1]["n"],
            n_distinct=rows[-1]["n_distinct"],
        )
    return rows, trades


def _chosen_params(family: str, chosen: Mapping[str, Any], version: str) -> SetupParams:
    """The family's defaults with the chosen grid row's values, labelled ``version``."""
    overrides: dict[str, Any] = dict(chosen["params"])
    return dataclasses.replace(DEFAULT_PARAMS[family], **overrides, version=version)


def _universe_size(ctx: ScanContext) -> dict[str, Any]:
    """The median number of eligible stocks per in-sample session, over how many sessions."""
    sizes = ctx.eligible.loc[pd.Timestamp(IS_START) :].sum(axis=1)
    median = float(sizes.median()) if len(sizes) else math.nan
    return {"median_eligible": median, "sessions": len(sizes)}


def _rules(params: object) -> object:
    """A pre-registration's parameters without the version label (what a revision must change)."""
    if isinstance(params, Mapping):
        return {k: v for k, v in params.items() if k != "version"}
    return params


def _refuse_identical_revision(
    repo: Path, family: str, version: str, params: Mapping[str, Any], protocol: object
) -> None:
    """:class:`PreregError` when an earlier version's pre-registration has these parameters (the
    version label aside) and this protocol block: a revision must change the rules, or it is
    just another look at an overlapping out-of-sample window. Earlier versions with no readable
    pre-registration on file have nothing to compare with and are skipped."""
    run = int(version.rsplit("-v", 1)[1])
    for index in range(1, run):
        earlier = f"{family}-v{index}"
        try:
            prereg = load_prereg(_validation_file(repo, family, earlier, "prereg"))
        except PreregError:
            continue
        if prereg.protocol == protocol and _rules(prereg.params) == _rules(params):
            raise PreregError(
                f"{version} is identical to {earlier}; a revision must change the rules"
            )


def run_insample(
    family: str, repo: Path, data: ValidationData | None = None, hypothesis: str = ""
) -> dict[str, Any]:
    """Run the in-sample grid, choose a variant, and write its draft pre-registration.

    Writes ``<version>.insample.json`` and a draft ``<version>.prereg.json``. The record holds
    every grid row (with its distinct-trade count), the chosen one and its trades' results by
    year, the median eligible universe over the in-sample window, the delisted coverage and the
    data fingerprint: what the checkpoint reviews before any out-of-sample run. The draft holds
    the chosen parameters, the protocol block, the gate, the windows and ``hypothesis``
    (required for a revision). Returns the record with ``params`` and both paths. ``data`` is
    loaded from the store when not given. Raises ``ValueError`` when no variant has enough
    trades or a report already on file cannot be read, and
    :class:`~swing_engine.validation.prereg.PreregError` when the chosen parameters and protocol
    are identical to an earlier version's; nothing is written then.
    """
    check_family(family)
    reports = _reports(repo, family)
    version = f"{family}-v{len(reports) + 1}"
    if data is None:
        data = asyncio.run(load_validation_data())

    ctx = _in_sample(data)
    rows, trades = _grid(ctx, family)
    chosen = select_variant(rows)
    chosen_trades = next(t for row, t in zip(rows, trades, strict=True) if row is chosen)
    params = _chosen_params(family, chosen, version)
    protocol = protocol_block(family)
    _refuse_identical_revision(repo, family, version, dataclasses.asdict(params), protocol)
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
        "chosen_by_year": _jsonable(by_year(chosen_trades)),
        "universe": _jsonable(_universe_size(ctx)),
        "coverage": _jsonable(data.coverage),
        "fingerprint": data_fingerprint(data.meta),
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
        protocol=protocol,
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
    """The pre-registration is this run's and declares the protocol's gate, windows and rules."""
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
    if prereg.protocol is None:
        raise PreregError(
            f"{where} has no protocol block (it was written before the rules outside the setup "
            "parameters were sealed): re-run insample to draft a new pre-registration"
        )
    if prereg.protocol != protocol_block(family):
        raise PreregError(
            f"{where} was registered under other rules (leader gate, universe, execution, risk, "
            "regime factor or grid) than the code has now: re-run insample"
        )


def freeze_session(panel: Panel) -> date:
    """The last SPY session, once its data looks fully loaded.

    Raises ``ValueError`` (saying what to run) when fewer than ``FREEZE_COVERAGE`` of the symbols
    that had a bar on the SPY session before have one on it: an update that stopped half way
    would otherwise freeze the window on a session most stocks seem to have skipped.
    """
    last = _last_spy_session(panel)
    sessions = panel.close[BENCHMARK].dropna().index
    if len(sessions) < 2:
        return last
    before = pd.Timestamp(sessions[-2])
    counts = panel.close.loc[[before, pd.Timestamp(last)]].notna().sum(axis=1).to_numpy()
    if counts[1] < FREEZE_COVERAGE * counts[0]:
        raise ValueError(
            f"the data looks incomplete on {last}: {counts[1]} symbols have a bar, against "
            f"{counts[0]} on {before.date()}; run `python -m swing_engine backfill "
            "--refresh-assets && python -m swing_engine update`, then retry"
        )
    return last


def other_family_params(repo: Path, family: str) -> tuple[SetupParams | None, str]:
    """The other family's parameters for the combined portfolio, and a note saying why.

    Only a family that has had its own out-of-sample run takes part, with the sealed parameters
    that run used: ``(None, reason)`` before that run (its window must not be looked at first)
    and when its pre-registration is missing or no longer the file its run recorded.
    """
    other = next(f for f in FAMILIES if f != check_family(family))
    report = latest_report(repo, other)
    if report is None:
        return None, OTHER_NOT_VALIDATED
    prereg = _sealed_prereg(repo, other, report["version"])
    if prereg is None or prereg.sha256 != report.get("prereg_sha256"):
        return None, OTHER_PREREG_CHANGED
    try:
        params = _setup_params(other, prereg.params)
    except TypeError:
        return None, OTHER_PREREG_CHANGED
    return params, f"with {report['version']} (its pre-registered parameters)"


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


def _daily_returns(result: PortfolioResult) -> npt.NDArray[np.float64]:
    equity = result.equity.to_numpy(dtype="float64")
    returns: npt.NDArray[np.float64] = equity[1:] / equity[:-1] - 1.0
    return returns


def _moments(returns: npt.NDArray[np.float64]) -> tuple[float, float]:
    """Sample skew and (non-excess) kurtosis of ``returns``; NaN when they are flat or too few."""
    if returns.size < 3 or not np.std(returns) > 0.0:
        return math.nan, math.nan
    return float(sps.skew(returns)), float(sps.kurtosis(returns, fisher=False))


def _baseline(seeds: npt.NDArray[np.float64]) -> dict[str, float]:
    finite = seeds[~np.isnan(seeds)]
    if not finite.size:
        return {"median": math.nan, "p5": math.nan, "p95": math.nan}
    p5, median, p95 = np.percentile(finite, [5, 50, 95])
    return {"median": float(median), "p5": float(p5), "p95": float(p95)}


def regime_filter_helps(
    downtrend_trades: int, sharpe_gated: float, sharpe_ungated: float, ungated_gate: Verdict
) -> bool:
    """Whether the regime filter keeps its place as a gate (spec §7.3).

    It becomes a warning (False) only on strong out-of-sample evidence that it hurt: at least
    ``MIN_REGIME_OFF_TRADES`` closed trades taken in a downtrend, the ungated setup-alone
    portfolio's daily Sharpe above the gated one's, AND the ungated configuration passing the
    full ship gate on its own numbers. Anything short of that, a NaN included, keeps the gate.
    """
    drop = (
        downtrend_trades >= MIN_REGIME_OFF_TRADES
        and sharpe_ungated > sharpe_gated
        and ungated_gate.passed
    )
    return not drop


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
    committed, unchanged and sealed, does not match this run, has no protocol block or one that
    is not the code's rules now, or repeats an earlier version's rules. Once the data is loaded
    it refuses (``ValueError``) a last session that looks half loaded, and once the in-sample
    grid is recomputed a pre-registration whose parameters are not the in-sample choice. The
    report (``<version>.report.json``) is written whatever the verdict, and returned as written.
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
    _refuse_identical_revision(repo, family, version, prereg.params, prereg.protocol)
    if data is None:
        data = asyncio.run(load_validation_data())

    freeze = freeze_session(data.panel)
    if freeze < oos_start:
        raise ValueError(f"no out-of-sample data: the last {BENCHMARK} session is {freeze}")
    grid, _ = _grid(_in_sample(data), family)
    chosen = select_variant(grid)
    params = _chosen_params(family, chosen, version)
    if prereg.params != dataclasses.asdict(params):
        raise PreregError(f"{version} is not the in-sample choice; re-run insample")
    is_stats = {k: v for k, v in chosen.items() if k != "params"}

    ctx = build_context(data.panel, data.kinds, freeze)  # cut at the freeze before any work

    def oos_trades(p: SetupParams) -> tuple[pd.DataFrame, pd.DataFrame]:
        candidates = context_candidates(ctx, p, oos_start)
        return candidates, simulate_candidates(
            ctx.panel, ctx.ind, candidates, p, _EX, data.delisted
        )

    candidates, trades = oos_trades(params)
    on = regime_on(trades)
    on_stats, off_stats = trade_stats(on), trade_stats(trades)
    downtrend_trades = off_stats["n"] - on_stats["n"]

    settings, close = RiskSettings(), ctx.panel.close
    alone = simulate_portfolio(trades, close, settings, oos_start, freeze)
    ungated = simulate_portfolio(
        trades.assign(regime="uptrend"), close, settings, oos_start, freeze
    )
    other_params, combined_note = other_family_params(repo, family)
    combined = None
    if other_params is not None:
        _, other_trades = oos_trades(other_params)
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

    returns = _daily_returns(alone)
    daily_sharpe = sharpe(returns, periods_per_year=1)
    ungated_sharpe = sharpe(_daily_returns(ungated), periods_per_year=1)
    skew, kurtosis = _moments(returns)
    n_trials = len(grid) + run_index
    gate = GateThresholds(**prereg.gate)
    verdict = evaluate_gate(
        on_stats, baseline["median"], alone.max_dd, spy.max_dd, data.coverage, gate
    )
    ungated_verdict = evaluate_gate(
        off_stats, baseline["median"], ungated.max_dd, spy.max_dd, data.coverage, gate
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
            **code_state(repo),
            "is_stats": is_stats,
            "grid": grid,
            "oos_trades": on_stats,
            "oos_trades_distinct": trade_stats(distinct_trades(on)),
            "oos_trades_regime_off": off_stats,
            "by_year": by_year(on),
            "baseline": baseline,
            "portfolio_alone": _portfolio(alone),
            "portfolio_combined": None if combined is None else _portfolio(combined),
            "portfolio_combined_note": combined_note,
            "spy": _portfolio(spy),
            "regime_filter": {
                "helps": regime_filter_helps(
                    downtrend_trades, daily_sharpe, ungated_sharpe, ungated_verdict
                ),
                "on": on_stats,
                "off": off_stats,
                "max_dd": {"on": alone.max_dd, "off": ungated.max_dd},
                "sharpe": {"on": daily_sharpe, "off": ungated_sharpe},
                "downtrend_trades": downtrend_trades,
                "ungated_gate": {
                    "passed": ungated_verdict.passed,
                    "failures": ungated_verdict.failures,
                },
            },
            "ranking": rs_ranking(on),
            "coverage": data.coverage,
            "fingerprint": data_fingerprint(data.meta),
            "deflated_sharpe": {
                "value": deflated_sharpe(
                    daily_sharpe, n_trials, int(returns.size), skew=skew, kurtosis=kurtosis
                ),
                "observed_sharpe": daily_sharpe,
                "n_obs": int(returns.size),
                "n_trials": n_trials,
                "skew": skew,
                "kurtosis": kurtosis,
            },
            "verdict": {"passed": verdict.passed, "failures": verdict.failures},
        }
    )
    _write_json(report_path, report)
    log.info("validation.oos_done", family=family, version=version, passed=verdict.passed)
    out: dict[str, Any] = report
    return out
