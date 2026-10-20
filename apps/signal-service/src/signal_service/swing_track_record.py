"""The swing track record, read from what is stored: the validation files and the live record.

:func:`read_families` reads ``ml/validation/<family>/`` exactly as Phase 1 writes it (the
verdicts of :func:`~swing_engine.validation.run.load_verdicts`, every out-of-sample report and the
in-sample files); :func:`family_out` adds the forward-tracked record of the stored ideas
(:func:`~swing_engine.tracking.live_stats`) and the warnings. Nothing here runs a backtest: the
numbers are the committed reports'. :func:`idea_record` is the short version shown beside each
idea on Today and on a stock page.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import numpy as np
from swing_engine.config import RiskSettings
from swing_engine.narrate import SETUP_LABEL
from swing_engine.settings import SwingSettings
from swing_engine.tracking import live_stats
from swing_engine.validation.prereg import PreregError, load_prereg
from swing_engine.validation.protocol import FAMILIES
from swing_engine.validation.run import VALIDATION_DIR, ReportError, family_reports, load_verdicts

Verdict = Literal["live", "research", "not_tested"]

UNDERPERFORMING_AFTER = 30
"""Closed live ideas before a negative live expectancy is called out on the track record."""


def as_float(value: Any) -> float | None:
    """A stored number as a float; None, NaN, infinities and the reports' ``"inf"`` as None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal, np.integer, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def as_int(value: Any) -> int | None:
    number = as_float(value)
    return None if number is None else int(number)


@dataclass(frozen=True)
class FamilyRecord:
    """One setup family's validation state, read from ``ml/validation``."""

    family: str
    verdict: Verdict
    verdict_text: str
    reports: list[dict[str, Any]]
    report: dict[str, Any] | None
    insample: dict[str, Any] | None
    insample_versions: list[str]
    start_equity: float


def _version_key(version: str) -> int:
    match = re.search(r"-v(\d+)$", version)
    return int(match.group(1)) if match else 0


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def read_families(repo: Path) -> dict[str, FamilyRecord]:
    """Per family: the verdict (``load_verdicts``), its reports and its in-sample stats."""
    verdicts = load_verdicts(repo)
    out: dict[str, FamilyRecord] = {}
    for family in FAMILIES:
        verdict = verdicts[family]
        failures = [str(f) for f in verdict["failures"]]
        if verdict["live"]:
            kind: Verdict = "live"
            text = "Live"
        else:
            kind = "not_tested" if failures == ["not yet tested"] else "research"
            text = f"Research only: {failures[0]}" if failures else "Research only"
        try:
            reports = family_reports(repo, family)
        except ReportError:
            reports = []
        report = reports[-1] if reports else None
        folder = repo / VALIDATION_DIR / family
        insample_files = sorted(
            folder.glob(f"{family}-v*.insample.json"),
            key=lambda p: _version_key(p.name.removesuffix(".insample.json")),
        )
        insample: dict[str, Any] | None = None
        if report is not None and isinstance(report.get("is_stats"), dict):
            insample = report["is_stats"]
        elif insample_files:
            doc = _read_json(insample_files[-1])
            chosen = None if doc is None else doc.get("chosen")
            if isinstance(chosen, dict):
                insample = {k: v for k, v in chosen.items() if k != "params"}
        out[family] = FamilyRecord(
            family=family,
            verdict=kind,
            verdict_text=text,
            reports=reports,
            report=report,
            insample=insample,
            insample_versions=[p.name.removesuffix(".insample.json") for p in insample_files],
            start_equity=_start_equity(repo, family, report),
        )
    return out


def _start_equity(repo: Path, family: str, report: Mapping[str, Any] | None) -> float:
    """The account the report's portfolio simulation started with: the risk settings sealed in
    its pre-registration's protocol block, else the code default (what ``run_oos`` uses)."""
    default = float(RiskSettings().equity)
    if report is None or not isinstance(report.get("version"), str):
        return default
    path = repo / VALIDATION_DIR / family / f"{report['version']}.prereg.json"
    try:
        protocol = load_prereg(path).protocol
    except PreregError:
        return default
    equity = as_float(((protocol or {}).get("risk") or {}).get("equity"))
    return default if equity is None or equity <= 0 else equity


def idea_record(fam: FamilyRecord | None) -> dict[str, Any]:
    """The track-record cell of an idea: out-of-sample stats, else in-sample, else nothing."""
    if fam is None:
        return {
            "verdict": "not_tested",
            "verdict_text": "Research only: not yet tested",
            "win_rate": None,
            "expectancy_r": None,
            "trades": None,
            "trades_distinct": None,
            "source": None,
        }
    oos = None if fam.report is None else fam.report.get("oos_trades")
    stats: Any = oos if isinstance(oos, dict) else fam.insample
    source: str | None = "oos" if isinstance(oos, dict) else "insample"
    if not isinstance(stats, dict):
        stats, source = {}, None
    return {
        "verdict": fam.verdict,
        "verdict_text": fam.verdict_text,
        "win_rate": as_float(stats.get("win_rate")),
        "expectancy_r": as_float(stats.get("expectancy_r")),
        "trades": as_int(stats.get("n")),
        "trades_distinct": as_int(stats.get("n_distinct")),
        "source": source,
    }


_STAT_FLOATS = ("win_rate", "avg_win_r", "avg_loss_r", "expectancy_r", "profit_factor", "avg_hold")


def stats_out(doc: Any) -> dict[str, Any] | None:
    """A ``trade_stats`` dict as stored (``"inf"`` kept as a string) or computed, for the API."""
    if not isinstance(doc, Mapping):
        return None
    out: dict[str, Any] = {
        "n": as_int(doc.get("n")) or 0,
        "n_distinct": as_int(doc.get("n_distinct")),
    }
    out.update({k: as_float(doc.get(k)) for k in _STAT_FLOATS})
    out["open"] = as_int(doc.get("open"))
    return out


def series_out(rows: Any, scale: float = 1.0) -> list[dict[str, Any]]:
    """``[[iso_date, value], ...]`` (a report's ``equity_weekly``) as chart points."""
    points = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, (list, tuple)) and len(row) == 2:
            value = as_float(row[1])
            if value is not None:
                points.append({"time": row[0], "value": value * scale})
    return points


def portfolio_out(doc: Any) -> dict[str, Any] | None:
    if not isinstance(doc, Mapping):
        return None
    return {
        "cagr": as_float(doc.get("cagr")),
        "sharpe": as_float(doc.get("sharpe")),
        "max_dd": as_float(doc.get("max_dd")),
        "exposure": as_float(doc.get("exposure")),
        "trades": as_int(doc.get("trades")),
    }


def family_out(
    fam: FamilyRecord, rows: Sequence[Mapping[str, Any]], settings: SwingSettings
) -> dict[str, Any]:
    """One family's track record: ``fam`` from :func:`read_families`, ``rows`` its stored ideas
    (``product_store.idea_rows``) for the live and reconstructed records and ``settings`` for
    the universe note. Nothing is recomputed: the backtest numbers are the committed report's."""
    live, rebuilt = live_stats(rows, reconstructed=False), live_stats(rows, reconstructed=True)
    report = fam.report or {}
    versions = [
        {
            "version": str(r.get("version")),
            "run_index": as_int(r.get("run_index")),
            "freeze_date": r.get("freeze_date"),
            "passed": bool((r.get("verdict") or {}).get("passed")),
        }
        for r in fam.reports
    ]
    tested = {v["version"] for v in versions}
    versions += [
        {"version": v, "run_index": None, "freeze_date": None, "passed": None}
        for v in fam.insample_versions
        if v not in tested
    ]
    warnings = []
    expectancy = as_float(live.get("expectancy_r"))
    if expectancy is not None and expectancy < 0 and live["n"] >= UNDERPERFORMING_AFTER:
        warnings.append(
            f"live underperforming: expectancy {expectancy:.2f}R after {live['n']} live ideas"
        )
    if fam.report is not None and settings.differs_from_default_universe():
        warnings.append("validated with default universe filters; yours differ")
    baseline = report.get("baseline")
    return {
        "family": fam.family,
        "label": SETUP_LABEL.get(fam.family, fam.family.capitalize()),
        "verdict": fam.verdict,
        "verdict_text": fam.verdict_text,
        "versions": versions,
        "insample": stats_out(fam.insample),
        "oos": stats_out(report.get("oos_trades")),
        "oos_distinct": stats_out(report.get("oos_trades_distinct")),
        "live": stats_out(live),
        "reconstructed": stats_out(rebuilt),
        "baseline": None
        if not isinstance(baseline, Mapping)
        else {k: as_float(baseline.get(k)) for k in ("median", "p5", "p95")},
        "equity": {
            "strategy": series_out((report.get("portfolio_alone") or {}).get("equity_weekly")),
            "spy": series_out(
                (report.get("spy") or {}).get("equity_weekly"), scale=fam.start_equity
            ),
        },
        "portfolio": {
            "strategy": portfolio_out(report.get("portfolio_alone")),
            "spy": portfolio_out(report.get("spy")),
            "combined": portfolio_out(report.get("portfolio_combined")),
            "combined_note": report.get("portfolio_combined_note"),
        },
        "warnings": warnings,
    }
