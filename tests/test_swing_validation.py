"""Swing engine: the validation harness, the gate between "hypothesis" and "Live" (spec §7.8).

The harness has to make overfitting hard, so most of these tests pin a refusal:

* the ship gate's failure messages, word for word, and the higher profit-factor floor when too
  few delisted names have data;
* the in-sample variant choice (only variants with 100+ trades, ties broken by profit factor);
* the pre-registration: the out-of-sample run refuses a file that is untracked, edited since its
  commit, tampered with (its sha256 no longer matches), or a revision without a hypothesis;
* the out-of-sample budget: 3 runs per family, then nothing until 12 months after the last
  freeze, and never the same version twice;
* the scan reads the committed verdicts: pre-registered parameters, Live or not, the regime gate
  turned into a warning when it did not help, and the proximity ranking when RS did not help.

The harness itself runs end to end on an injected synthetic universe whose dates span the
in-sample and out-of-sample windows, in a real temporary git repo. Two leakage tests scramble
the data after ``IS_END`` (in-sample) and after the freeze (out-of-sample) and require identical
results. Nothing here needs Postgres: the store is faked where the data loader is tested.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import subprocess
from contextlib import redirect_stdout
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import swing_engine.__main__ as cli
from swing_engine import store
from swing_engine.config import BreakoutParams, PullbackParams
from swing_engine.data.assets import AssetKind
from swing_engine.panel import Panel
from swing_engine.scan import default_setups
from swing_engine.testing import scramble_after, synthetic_kinds, synthetic_universe_panel
from swing_engine.validation.prereg import (
    Prereg,
    PreregError,
    check_committed,
    write_draft,
)
from swing_engine.validation.protocol import (
    GRIDS,
    IS_END,
    IS_START,
    OOS_START,
    GateThresholds,
    Verdict,
    evaluate_gate,
    grid_variants,
    select_variant,
)
from swing_engine.validation.run import (
    BudgetError,
    ValidationData,
    check_budget,
    data_fingerprint,
    load_validation_data,
    load_verdicts,
    panel_meta,
    params_for,
    ranking_choice,
    run_insample,
    run_oos,
)

NOW = datetime(2026, 10, 19, 21, 0, tzinfo=UTC)
_FIELDS = ("open", "high", "low", "close", "volume")


# ---------------------------------------------------------------------------- the gate


def test_gate_failure_messages_exact():
    v = evaluate_gate(
        {"expectancy_r": 0.1, "profit_factor": 1.05, "n": 80}, 0.15, -0.30, -0.25, coverage=0.95
    )
    assert v.failures == [
        "profit factor 1.05 < 1.20",
        "only 80 trades (need 100)",
        "expectancy 0.10R not above random entry 0.15R",
        "drawdown 30% worse than SPY 25%",
    ]
    assert v.passed is False


def test_low_coverage_raises_profit_factor_floor():
    oos = {"expectancy_r": 0.3, "profit_factor": 1.25, "n": 200}
    assert "profit factor 1.25 < 1.30" in evaluate_gate(oos, 0.0, -0.1, -0.2, 0.80).failures
    assert evaluate_gate(oos, 0.0, -0.1, -0.2, 0.90) == Verdict(passed=True, failures=[])


def test_gate_fails_a_non_positive_expectancy_first():
    oos = {"expectancy_r": -0.04, "profit_factor": 0.9, "n": 150}
    v = evaluate_gate(oos, -0.2, -0.1, -0.2, 1.0)
    assert v.failures == ["expectancy -0.04R ≤ 0", "profit factor 0.90 < 1.20"]


def test_gate_drawdown_equal_to_spy_is_not_worse():
    oos = {"expectancy_r": 0.2, "profit_factor": 1.5, "n": 100}
    assert evaluate_gate(oos, 0.1, -0.25, -0.25, 1.0).passed
    assert evaluate_gate(oos, 0.1, -0.26, -0.25, 1.0).failures == [
        "drawdown 26% worse than SPY 25%"
    ]


def test_gate_with_no_trades_fails_every_trade_condition():
    nan = float("nan")
    v = evaluate_gate({"expectancy_r": nan, "profit_factor": nan, "n": 0}, nan, 0.0, -0.1, 1.0)
    assert not v.passed
    assert v.failures[2] == "only 0 trades (need 100)" and len(v.failures) == 4


def test_gate_thresholds_are_the_spec_defaults():
    t = GateThresholds()
    assert (t.min_expectancy_r, t.min_profit_factor, t.min_profit_factor_low_coverage) == (
        0.0,
        1.2,
        1.3,
    )
    assert (t.coverage_floor, t.min_trades) == (0.90, 100)


# ---------------------------------------------------------------------------- the grid


def test_grid_variants_are_the_cartesian_product_in_key_order():
    breakout = grid_variants("breakout")
    assert len(breakout) == 9
    assert breakout[:4] == [
        {"stop_atr": 1.5, "trail_atr": 2.5},
        {"stop_atr": 1.5, "trail_atr": 3.0},
        {"stop_atr": 1.5, "trail_atr": 3.5},
        {"stop_atr": 2.0, "trail_atr": 2.5},
    ]
    pullback = grid_variants("pullback")
    assert len(pullback) == 6 and pullback[1] == {"trail_atr": 2.5, "stop_buffer_atr": 0.5}
    assert set(GRIDS) == {"breakout", "pullback"}
    with pytest.raises(ValueError, match="unknown setup family"):
        grid_variants("momentum")


def test_select_variant_requires_100_trades_and_breaks_ties_by_pf():
    rows = [
        {"params": {"stop_atr": 1.5}, "n": 99, "expectancy_r": 0.90, "profit_factor": 3.0},
        {"params": {"stop_atr": 2.0}, "n": 100, "expectancy_r": 0.30, "profit_factor": 1.4},
        {"params": {"stop_atr": 2.5}, "n": 150, "expectancy_r": 0.30, "profit_factor": 1.6},
        {"params": {"stop_atr": 3.0}, "n": 400, "expectancy_r": 0.20, "profit_factor": 2.5},
    ]
    assert select_variant(rows)["params"] == {"stop_atr": 2.5}
    with pytest.raises(ValueError) as err:
        select_variant([{**rows[0]}, {**rows[1], "n": 12}])
    assert str(err.value) == "no variant has ≥100 in-sample trades"


# ---------------------------------------------------------------------------- pre-registration


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, *paths: Path) -> None:
    _git(repo, "add", *(str(p.relative_to(repo)) for p in paths))
    _git(repo, "commit", "--no-verify", "-q", "-m", "test")


def _init_repo(path: Path) -> Path:
    """A new, empty git repo at ``path`` with a local identity (nothing global is touched)."""
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    return path


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path / "repo")
    (repo / "README").write_text("validation test repo\n")
    _commit(repo, repo / "README")
    return repo


def _prereg(family: str = "breakout", version: str = "breakout-v1", **over) -> Prereg:
    params = {"breakout": BreakoutParams, "pullback": PullbackParams}[family](version=version)
    fields = dict(
        family=family, version=version, params=dataclasses.asdict(params),
        gate=dataclasses.asdict(GateThresholds()),
        is_window=[IS_START.isoformat(), IS_END.isoformat()],
        oos_start=OOS_START.isoformat(), hypothesis="", created_at="2026-10-19T21:00:00+00:00",
    )  # fmt: skip
    return Prereg.create(**{**fields, **over})


def _prereg_path(repo: Path, family: str, version: str) -> Path:
    return repo / "ml" / "validation" / family / f"{version}.prereg.json"


def test_prereg_sha256_is_the_digest_of_every_other_field():
    p = _prereg()
    fields = {k: v for k, v in dataclasses.asdict(p).items() if k != "sha256"}
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    assert p.sha256 == hashlib.sha256(canonical.encode()).hexdigest()
    assert _prereg(hypothesis="tighter stops").sha256 != p.sha256


def test_a_committed_prereg_round_trips(tmp_git_repo):
    path = _prereg_path(tmp_git_repo, "breakout", "breakout-v1")
    write_draft(path, _prereg())
    _commit(tmp_git_repo, path)
    assert check_committed(path, tmp_git_repo) == _prereg()


def test_prereg_must_be_committed(tmp_git_repo):
    untracked_path = _prereg_path(tmp_git_repo, "breakout", "breakout-v1")
    write_draft(untracked_path, _prereg())
    with pytest.raises(PreregError, match="not committed"):
        check_committed(untracked_path, tmp_git_repo)
    _git(tmp_git_repo, "add", str(untracked_path.relative_to(tmp_git_repo)))  # staged only
    with pytest.raises(PreregError, match="differs from HEAD"):
        check_committed(untracked_path, tmp_git_repo)


def test_a_prereg_missing_or_edited_after_its_commit_is_refused(tmp_git_repo):
    path = _prereg_path(tmp_git_repo, "breakout", "breakout-v1")
    with pytest.raises(PreregError, match="no pre-registration"):
        check_committed(path, tmp_git_repo)
    write_draft(path, _prereg())
    _commit(tmp_git_repo, path)
    write_draft(path, _prereg(params=dataclasses.asdict(BreakoutParams(stop_atr=2.5))))
    with pytest.raises(PreregError, match="differs from HEAD"):
        check_committed(path, tmp_git_repo)


def test_a_tampered_prereg_is_refused_even_when_committed(tmp_git_repo):
    path = _prereg_path(tmp_git_repo, "breakout", "breakout-v1")
    write_draft(path, _prereg())
    doc = json.loads(path.read_text())
    doc["params"]["stop_atr"] = 2.5  # hand-edited, sha256 left as it was
    path.write_text(json.dumps(doc))
    _commit(tmp_git_repo, path)
    with pytest.raises(PreregError, match="sha256"):
        check_committed(path, tmp_git_repo)


def test_revision_requires_hypothesis(tmp_git_repo):
    path = _prereg_path(tmp_git_repo, "breakout", "breakout-v2")
    write_draft(path, _prereg(version="breakout-v2", hypothesis="  "))
    _commit(tmp_git_repo, path)
    with pytest.raises(PreregError, match="hypothesis"):
        check_committed(path, tmp_git_repo)

    revised = _prereg(version="breakout-v2", hypothesis="IS losers cluster in wide bases")
    write_draft(path, revised)
    _commit(tmp_git_repo, path)
    assert check_committed(path, tmp_git_repo) == revised


# ---------------------------------------------------------------------------- reports on disk


def _write_report(
    repo: Path,
    family: str,
    n: int,
    *,
    passed: bool,
    freeze: str = "2026-09-30",
    helps: bool = True,
    rs_helps: bool = True,
) -> Path:
    path = repo / "ml" / "validation" / family / f"{family}-v{n}.report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "family": family,
        "version": f"{family}-v{n}",
        "run_index": n,
        "freeze_date": freeze,
        "regime_filter": {"helps": helps},
        "ranking": {"rs_helps": rs_helps},
        "verdict": {"passed": passed, "failures": [] if passed else ["only 80 trades (need 100)"]},
    }
    path.write_text(json.dumps(report))
    return path


def test_budget_refuses_fourth_oos_run(tmp_path):
    for n in (1, 2, 3):
        _write_report(tmp_path, "breakout", n, passed=False, freeze="2026-06-30")
    with pytest.raises(BudgetError, match="budget"):
        run_oos("breakout", "breakout-v4", tmp_path, NOW)  # refused before any data is read
    with pytest.raises(BudgetError, match="2027-06-30"):
        check_budget(tmp_path, "breakout", NOW)


def test_budget_reopens_12_months_after_the_last_freeze(tmp_path):
    assert check_budget(tmp_path, "breakout", NOW) == (1, OOS_START)
    _write_report(tmp_path, "breakout", 1, passed=False, freeze="2025-08-29")
    _write_report(tmp_path, "breakout", 2, passed=False, freeze="2025-09-30")
    assert check_budget(tmp_path, "breakout", NOW) == (3, OOS_START)
    _write_report(tmp_path, "breakout", 3, passed=False, freeze="2025-10-17")
    assert check_budget(tmp_path, "breakout", NOW) == (4, date(2025, 10, 18))  # 12 months on
    assert check_budget(tmp_path, "pullback", NOW) == (1, OOS_START)  # budgets are per family
    with pytest.raises(BudgetError):
        check_budget(tmp_path, "breakout", datetime(2026, 10, 16, tzinfo=UTC))


def test_a_version_runs_out_of_sample_only_once(tmp_path):
    _write_report(tmp_path, "breakout", 1, passed=False)
    with pytest.raises(BudgetError, match="already"):
        run_oos("breakout", "breakout-v1", tmp_path, NOW)


def test_load_verdicts_defaults_to_not_yet_tested(tmp_path):
    untested = {"live": False, "version": None, "failures": ["not yet tested"], "report_path": None}
    assert load_verdicts(tmp_path)["breakout"] == untested
    assert list(load_verdicts(tmp_path)) == ["breakout", "pullback"]


def test_load_verdicts_reads_the_latest_run_of_each_family(tmp_path):
    _write_report(tmp_path, "breakout", 1, passed=True)
    _write_report(tmp_path, "breakout", 2, passed=False)
    _write_report(tmp_path, "pullback", 1, passed=True)
    verdicts = load_verdicts(tmp_path)
    assert verdicts["breakout"] == {
        "version": "breakout-v2",
        "live": False,
        "failures": ["only 80 trades (need 100)"],
        "report_path": "ml/validation/breakout/breakout-v2.report.json",
    }
    assert verdicts["pullback"]["live"] is True and verdicts["pullback"]["failures"] == []


def test_params_for_reads_the_prereg_or_falls_back_to_defaults(tmp_path):
    assert params_for("breakout", None, tmp_path) == BreakoutParams()
    assert params_for("pullback", "pullback-v2", tmp_path) == PullbackParams()  # no file
    p = _prereg(params=dataclasses.asdict(BreakoutParams(stop_atr=2.5, trail_atr=3.5)))
    write_draft(_prereg_path(tmp_path, "breakout", "breakout-v1"), p)
    assert params_for("breakout", "breakout-v1", tmp_path) == BreakoutParams(
        stop_atr=2.5, trail_atr=3.5
    )


def test_default_setups_use_preregistered_params_and_verdict(tmp_path):
    p = _prereg(params=dataclasses.asdict(BreakoutParams(stop_atr=2.5)))
    write_draft(_prereg_path(tmp_path, "breakout", "breakout-v1"), p)
    _write_report(tmp_path, "breakout", 1, passed=True)

    specs = default_setups(tmp_path)
    spec = {s.params.version: s for s in specs}["breakout-v1"]
    assert spec.live and spec.params.stop_atr == 2.5 and spec.regime_gate
    assert [type(s.params) for s in specs] == [BreakoutParams, PullbackParams]
    assert specs[1].params == PullbackParams() and not specs[1].live and specs[1].regime_gate


def test_regime_filter_that_does_not_help_turns_into_warning(tmp_path):
    write_draft(_prereg_path(tmp_path, "breakout", "breakout-v1"), _prereg())
    _write_report(tmp_path, "breakout", 1, passed=True, helps=False)
    spec = {s.params.version: s for s in default_setups(tmp_path)}["breakout-v1"]
    assert spec.regime_gate is False


def test_ranking_choice_falls_back_to_proximity_when_rs_does_not_help(tmp_path):
    assert ranking_choice(tmp_path) == "rs"  # nothing validated yet
    _write_report(tmp_path, "pullback", 1, passed=False, rs_helps=False)
    assert ranking_choice(tmp_path) == "rs"  # only a Live family's ranking counts
    _write_report(tmp_path, "breakout", 1, passed=True, rs_helps=False)
    assert ranking_choice(tmp_path) == "proximity"


# ---------------------------------------------------------------------------- data


def _shifted(panel: Panel, start: str) -> Panel:
    """``panel`` on consecutive business days from ``start`` (the values are unchanged)."""
    index = pd.bdate_range(start, periods=len(panel.dates()))
    frames = {f: getattr(panel, f).set_axis(index) for f in (*_FIELDS, "raw_close")}
    return Panel(**frames)


def _data(panel: Panel, delisted: frozenset[str] = frozenset()) -> ValidationData:
    return ValidationData(
        panel=panel,
        kinds=synthetic_kinds(panel),
        delisted=delisted,
        coverage=0.95,
        meta=panel_meta(panel),
    )


def test_panel_meta_and_fingerprint_describe_the_data():
    panel = synthetic_universe_panel(seed=2, n_symbols=3, n_sessions=300)
    panel.close.iloc[-5:, 1] = np.nan  # S00 stops five sessions early
    meta = panel_meta(panel)
    last = panel.dates()[-1].date().isoformat()
    assert meta["last_bars"]["SPY"] == last
    assert meta["last_bars"]["S00"] == panel.dates()[-6].date().isoformat()
    assert meta["rows"] == 4 * 300 - 5
    assert (meta["first_date"], meta["last_date"]) == (panel.dates()[0].date().isoformat(), last)

    fp = data_fingerprint(meta)
    listing = "\n".join(f"{s}:{d}" for s, d in sorted(meta["last_bars"].items()))
    assert fp == {
        "symbols": 4,
        "first_date": meta["first_date"],
        "last_date": last,
        "rows": 4 * 300 - 5,
        "sha256": hashlib.sha256(listing.encode()).hexdigest(),
    }
    moved = {**meta, "last_bars": {**meta["last_bars"], "S00": last}}
    assert data_fingerprint(moved)["sha256"] != fp["sha256"]


def _long_bars(panel: Panel, symbols, start: date, end: date, raw: bool) -> pd.DataFrame:
    """The ``store.load_daily_bars`` shape for ``panel``'s rows from ``start`` to ``end``."""
    frames = []
    for symbol in symbols:
        if symbol not in panel.close.columns:
            continue
        rows = pd.DataFrame({f: getattr(panel, f)[symbol] for f in _FIELDS})
        rows = rows.loc[pd.Timestamp(start) : pd.Timestamp(end)].dropna()
        if raw:
            rows["close"] = rows["close"] * 2.0  # unadjusted closes differ from adjusted ones
        rows.insert(0, "symbol", symbol)
        rows.insert(0, "ts", rows.index.tz_localize("UTC"))
        frames.append(rows.reset_index(drop=True))
    return pd.concat(frames, ignore_index=True)


async def test_load_validation_data_reads_the_store(monkeypatch):
    panel = _shifted(synthetic_universe_panel(seed=2, n_symbols=3, n_sessions=300), "2016-01-04")
    calls: dict[str, list] = {"prefilter": [], "bars": []}
    assets = pd.DataFrame(
        {
            "symbol": ["DEAD1", "DEAD2", "S00", "S01", "S02", "SPY", "WARR"],
            "status": ["inactive", "inactive", "active", "active", "active", "active", "inactive"],
            "kind": ["common", "common", "common", "common", "common", "etf", "warrant"],
        }
    )

    async def liquid_prefilter(min_dollar_volume: float) -> list[str]:
        calls["prefilter"].append(min_dollar_volume)
        return ["S00", "S01", "S02"]

    async def load_daily_bars(symbols, start, end, resolution="1d") -> pd.DataFrame:
        calls["bars"].append((list(symbols), start, end, resolution))
        return _long_bars(panel, symbols, start, end, raw=resolution == "1d_raw")

    async def load_assets() -> pd.DataFrame:
        return assets

    async def last_bar_dates(resolution: str = "1d") -> dict[str, date]:
        return {"DEAD1": date(2018, 3, 1), "S00": date(2017, 2, 1), "SPY": date(2017, 2, 1)}

    for name, fake in [
        ("liquid_prefilter", liquid_prefilter),
        ("load_daily_bars", load_daily_bars),
        ("load_assets", load_assets),
        ("last_bar_dates", last_bar_dates),
    ]:
        monkeypatch.setattr(store, name, fake)

    data = await load_validation_data()

    assert calls["prefilter"] == [20_000_000.0]
    assert [c[3] for c in calls["bars"]] == ["1d", "1d_raw"]
    for symbols, start, end, _ in calls["bars"]:
        assert symbols == ["S00", "S01", "S02", "SPY"] and start == IS_START
        assert panel.dates()[-1].date() <= end <= datetime.now(UTC).date()
    same = {"check_like": True, "check_freq": False}  # columns come back sorted
    pd.testing.assert_frame_equal(data.panel.close, panel.close, **same)
    assert data.panel.raw_close is not None
    pd.testing.assert_frame_equal(data.panel.raw_close, panel.close * 2.0, **same)
    assert data.kinds["SPY"] is AssetKind.ETF and data.kinds["S01"] is AssetKind.COMMON
    assert data.delisted == frozenset({"DEAD1", "DEAD2", "WARR"})
    assert data.coverage == 0.5  # DEAD1 of DEAD1/DEAD2; the warrant is not common stock
    assert data.meta == panel_meta(data.panel)


# ---------------------------------------------------------------------------- end to end


def _validation_panel(seed: int = 3) -> Panel:
    """40 stocks plus SPY from mid-2020 to mid-2023: 2.5 years in sample, 7 months out of it."""
    return _shifted(synthetic_universe_panel(seed=seed, n_symbols=40, n_sessions=800), "2020-06-01")


REPORT_KEYS = {
    "family", "version", "prereg_sha256", "run_index", "n_trials", "freeze_date", "oos_window",
    "is_stats", "grid", "oos_trades", "oos_trades_regime_off", "by_year", "baseline",
    "portfolio_alone", "portfolio_combined", "spy", "coverage", "fingerprint", "deflated_sharpe",
    "verdict", "regime_filter", "ranking",
}  # fmt: skip


@pytest.fixture(scope="module")
def validated(tmp_path_factory) -> dict:
    """``run_insample`` then ``run_oos`` for breakout in a fresh git repo, run once per module."""
    repo = _init_repo(tmp_path_factory.mktemp("e2e"))
    panel = _validation_panel()
    data = _data(panel, delisted=frozenset({"S07"}))

    summary = run_insample("breakout", repo, data=data)
    prereg = Path(summary["prereg_path"])
    with pytest.raises(PreregError, match="not committed"):
        run_oos("breakout", summary["version"], repo, NOW, data=data)
    _commit(repo, prereg)
    report = run_oos("breakout", summary["version"], repo, NOW, data=data)
    return {"repo": repo, "data": data, "summary": summary, "report": report}


def test_insample_writes_the_grid_and_a_draft_prereg(validated):
    repo, summary = validated["repo"], validated["summary"]
    assert summary["version"] == "breakout-v1"
    grid = json.loads((repo / "ml/validation/breakout/breakout-v1.insample.json").read_text())
    assert grid["is_window"] == [IS_START.isoformat(), IS_END.isoformat()]
    assert [row["params"] for row in grid["grid"]] == grid_variants("breakout")
    assert grid["chosen"] == select_variant(grid["grid"]) and grid["chosen"]["n"] >= 100

    prereg = json.loads(Path(summary["prereg_path"]).read_text())
    chosen = BreakoutParams(version="breakout-v1", **grid["chosen"]["params"])
    assert prereg["params"] == dataclasses.asdict(chosen)
    assert prereg["gate"] == dataclasses.asdict(GateThresholds())
    assert prereg["oos_start"] == OOS_START.isoformat() and prereg["hypothesis"] == ""


def test_oos_report_has_every_key_and_reports_the_verdict_either_way(validated):
    repo, report, data = validated["repo"], validated["report"], validated["data"]
    path = repo / "ml/validation/breakout/breakout-v1.report.json"
    assert json.loads(path.read_text()) == report
    assert set(report) == REPORT_KEYS

    freeze = data.panel.dates()[-1].date().isoformat()
    assert report["freeze_date"] == freeze
    assert report["oos_window"] == [OOS_START.isoformat(), freeze]
    assert (report["run_index"], report["n_trials"]) == (1, 9 + 1)
    prereg = json.loads((repo / "ml/validation/breakout/breakout-v1.prereg.json").read_text())
    assert report["prereg_sha256"] == prereg["sha256"]
    insample = json.loads((repo / "ml/validation/breakout/breakout-v1.insample.json").read_text())
    assert report["grid"] == insample["grid"]
    assert report["fingerprint"] == data_fingerprint(data.meta)
    assert report["coverage"] == 0.95

    verdict = report["verdict"]
    assert isinstance(verdict["passed"], bool) and isinstance(verdict["failures"], list)
    assert verdict["passed"] == (verdict["failures"] == [])
    assert report["oos_trades"]["n"] > 0
    baseline = report["baseline"]
    assert set(baseline) == {"median", "p5", "p95"}
    assert baseline["p5"] <= baseline["median"] <= baseline["p95"]
    assert report["deflated_sharpe"]["n_trials"] == 10
    assert set(report["regime_filter"]) == {"helps", "on", "off"}
    assert report["regime_filter"]["on"] == report["oos_trades"]
    assert report["regime_filter"]["off"] == report["oos_trades_regime_off"]
    assert isinstance(report["ranking"]["rs_helps"], bool)
    assert 1 <= len(report["ranking"]["quintile_expectancy"]) <= 5
    for year in report["by_year"]:
        assert int(year) >= OOS_START.year

    for name in ("portfolio_alone", "portfolio_combined", "spy"):
        weekly = report[name]["equity_weekly"]
        assert weekly[0][0] >= OOS_START.isoformat() and weekly[-1][0] >= freeze
        assert all(pd.Timestamp(d).day_name() == "Friday" for d, _ in weekly)
    assert report["spy"]["equity_weekly"][0][1] == pytest.approx(1.0, abs=0.05)
    assert report["portfolio_alone"]["max_dd"] <= 0.0


def test_oos_verdict_is_what_the_gate_says_about_the_reported_numbers(validated):
    report = validated["report"]
    nan = float("nan")
    oos = {k: nan if v is None else v for k, v in report["oos_trades"].items()}
    median = report["baseline"]["median"]
    expected = evaluate_gate(
        oos,
        nan if median is None else median,
        report["portfolio_alone"]["max_dd"],
        report["spy"]["max_dd"],
        report["coverage"],
    )
    assert report["verdict"] == {"passed": expected.passed, "failures": expected.failures}


def test_the_scan_reads_the_new_verdict(validated):
    repo, report = validated["repo"], validated["report"]
    verdict = load_verdicts(repo)["breakout"]
    assert verdict["version"] == "breakout-v1" and verdict["live"] == report["verdict"]["passed"]
    spec = default_setups(repo)[0]
    assert spec.params == params_for("breakout", "breakout-v1", repo)
    assert spec.regime_gate == report["regime_filter"]["helps"]


def test_the_same_version_cannot_run_out_of_sample_again(validated):
    with pytest.raises(BudgetError, match="already"):
        run_oos("breakout", "breakout-v1", validated["repo"], NOW, data=validated["data"])


def test_insample_never_sees_data_after_is_end(tmp_path):
    panel = _validation_panel()
    cut = panel.dates()[panel.dates() <= pd.Timestamp(IS_END)][-1]
    scrambled = scramble_after(panel, cut, seed=11)
    # Today's delisted flags are news from after IS_END too: had the in-sample run used them,
    # every trade still open at the cut would close there as "delisted" and count.
    everyone = frozenset(s for s in panel.symbols() if s != "SPY")
    runs = []
    for name, p, delisted in (("plain", panel, frozenset()), ("later", scrambled, everyone)):
        repo = _init_repo(tmp_path / name)
        run_insample("pullback", repo, data=_data(p, delisted))
        path = repo / "ml/validation/pullback/pullback-v1.insample.json"
        runs.append(json.loads(path.read_text()))
    assert runs[0]["grid"] == runs[1]["grid"] and runs[0]["chosen"] == runs[1]["chosen"]


def test_oos_never_sees_data_after_the_freeze(tmp_path):
    panel = _validation_panel()
    freeze = panel.dates()[-6]
    panel.close.loc[panel.dates() > freeze, "SPY"] = np.nan  # SPY's last five sessions are missing
    scrambled = scramble_after(panel, freeze, seed=12)
    scrambled.close.loc[scrambled.dates() > freeze, "SPY"] = np.nan
    reports = []
    for name, p in (("plain", panel), ("scrambled", scrambled)):
        repo = _init_repo(tmp_path / name)
        path = _prereg_path(repo, "pullback", "pullback-v1")
        write_draft(path, _prereg("pullback", "pullback-v1"))
        _commit(repo, path)
        reports.append(run_oos("pullback", "pullback-v1", repo, NOW, data=_data(p)))
    assert reports[0]["freeze_date"] == freeze.date().isoformat()
    assert reports[0] == reports[1]


# ---------------------------------------------------------------------------- the CLI


class _Calls:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[object] = []
        monkeypatch.setattr(cli, "load_env", lambda: self.events.append("load_env"))
        monkeypatch.setattr(cli, "setup_logging", lambda: self.events.append("setup_logging"))


def _main(*argv: str) -> str:
    with redirect_stdout(io.StringIO()) as out:
        cli.main(list(argv))
    return out.getvalue()


def test_insample_command_prints_the_grid_and_the_chosen_variant(monkeypatch):
    calls = _Calls(monkeypatch)
    row = {"n": 240, "win_rate": 0.41, "expectancy_r": 0.31, "profit_factor": 1.62, "avg_hold": 9.4}
    grid = [
        {"params": {"stop_atr": 1.5, "trail_atr": 2.5}, **row},
        {"params": {"stop_atr": 2.0, "trail_atr": 3.0}, **row, "n": 80, "profit_factor": None},
    ]

    def fake(family: str, repo: Path, data=None, hypothesis: str = "") -> dict:
        calls.events.append((family, repo, hypothesis))
        return {
            "family": family, "version": "breakout-v1", "grid": grid, "chosen": grid[0],
            "insample_path": "ml/validation/breakout/breakout-v1.insample.json",
            "prereg_path": "ml/validation/breakout/breakout-v1.prereg.json",
        }  # fmt: skip

    monkeypatch.setattr(cli, "run_insample", fake)
    out = _main("insample", "--family", "breakout", "--hypothesis", "wider trail")

    assert calls.events == ["load_env", "setup_logging", ("breakout", Path.cwd(), "wider trail")]
    for header in ("stop_atr", "trail_atr", "Trades", "Expectancy", "PF"):
        assert header in out
    assert "0.31" in out and "1.62" in out and "n/a" in out
    assert "chosen" in out.lower() and "stop_atr=1.5" in out
    assert "breakout-v1.prereg.json" in out and "commit" in out.lower()


def test_oos_command_prints_the_verdict_and_its_failures(monkeypatch):
    _Calls(monkeypatch)
    seen = {}

    def fake(family: str, version: str, repo: Path, now: datetime, data=None) -> dict:
        seen.update(family=family, version=version, repo=repo, now=now)
        return {
            "family": family, "version": version, "oos_window": ["2023-01-01", "2026-10-16"],
            "oos_trades": {"n": 80, "expectancy_r": 0.1, "profit_factor": 1.05},
            "baseline": {"median": 0.15}, "portfolio_alone": {"max_dd": -0.30},
            "spy": {"max_dd": -0.25}, "deflated_sharpe": {"value": 0.61}, "coverage": 0.93,
            "verdict": {"passed": False, "failures": ["only 80 trades (need 100)"]},
        }  # fmt: skip

    monkeypatch.setattr(cli, "run_oos", fake)
    out = _main("oos", "--family", "pullback", "--version", "pullback-v1")

    assert (seen["family"], seen["version"]) == ("pullback", "pullback-v1")
    assert seen["repo"] == Path.cwd()
    assert seen["now"].tzinfo is not None
    assert "FAILED" in out and "Research only" in out and "only 80 trades (need 100)" in out
    assert "pullback-v1.report.json" in out


@pytest.mark.parametrize(
    "error", [BudgetError("budget spent"), PreregError("not committed"), ValueError("no data")]
)
def test_validation_commands_exit_1_with_the_reason(monkeypatch, error):
    _Calls(monkeypatch)

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli, "run_oos", boom)
    monkeypatch.setattr(cli, "run_insample", boom)
    for argv in (["oos", "--family", "breakout", "--version", "breakout-v1"],
                 ["insample", "--family", "breakout"]):  # fmt: skip
        with pytest.raises(SystemExit) as exit_:
            _main(*argv)
        assert exit_.value.code == f"error: {error}"


def test_validation_arguments_are_checked(capsys):
    parser = cli.build_parser()
    assert parser.parse_args(["insample", "--family", "pullback"]).hypothesis == ""
    for argv in (["insample", "--family", "momentum"], ["oos", "--family", "breakout"]):
        with pytest.raises(SystemExit) as exit_:
            parser.parse_args(argv)
        assert exit_.value.code == 2
    assert "momentum" in capsys.readouterr().err


def test_a_malformed_version_is_refused_before_any_file_is_read(tmp_path):
    with pytest.raises(ValueError, match="breakout-v1"):
        run_oos("breakout", "../../etc/breakout-v1", tmp_path, NOW)
    assert not (tmp_path / "ml").exists()
