"""Swing engine: the validation harness, the gate between "hypothesis" and "Live" (spec §7.8).

The harness has to make overfitting hard, so most of these tests pin a refusal:

* the ship gate's failure messages, word for word, and the higher profit-factor floor when too
  few delisted names have data; the trade floor counts distinct trades (one open trade per
  symbol), not every re-trigger of a stock that sat near its high;
* the in-sample variant choice (only variants with 100+ trades, ties broken by profit factor);
* the pre-registration: the out-of-sample run refuses a file that is untracked, edited since its
  commit, tampered with (its sha256 no longer matches), a revision without a hypothesis, one
  sealed under other rules (leader gate, universe, fills, risk, regime factor, grid) than the
  code has now, or a revision whose rules are identical to an earlier version's;
* the out-of-sample budget: 3 runs per family, then nothing until 12 months after the last
  freeze, and never the same version twice;
* the scan reads the committed verdicts: pre-registered parameters, Live or not (and not Live once
  the rules changed after the run), the regime gate turned into a warning only on strong evidence,
  and the proximity ranking when RS did not help;
* the combined portfolio includes the other family only after its own out-of-sample run, with
  its pre-registered parameters, so one family's report never peeks at the other's window.

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
import math
import subprocess
from contextlib import redirect_stdout
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import swing_engine.__main__ as cli
import swing_engine.backtest.trades as trades_module
import swing_engine.validation.run as run_module
from backtest_service.metrics import deflated_sharpe
from scipy import stats as sps
from swing_engine import store
from swing_engine.backtest.portfolio import simulate_portfolio
from swing_engine.backtest.stats import by_year, distinct_trades, regime_on, trade_stats
from swing_engine.backtest.trades import candidate_history, simulate_candidates
from swing_engine.config import (
    REGIME_FACTOR,
    BreakoutParams,
    ExecutionParams,
    LeaderParams,
    PullbackParams,
    RiskSettings,
    UniverseFilter,
)
from swing_engine.data.assets import AssetKind
from swing_engine.indicators import compute_indicators
from swing_engine.panel import Panel, truncate
from swing_engine.scan import build_context, default_setups, json_safe
from swing_engine.testing import scramble_after, synthetic_kinds, synthetic_universe_panel
from swing_engine.validation.prereg import (
    Prereg,
    PreregError,
    check_committed,
    load_prereg,
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
    protocol_block,
    select_variant,
)
from swing_engine.validation.run import (
    RULES_CHANGED,
    RULES_UNSEALED,
    BudgetError,
    ValidationData,
    check_budget,
    data_fingerprint,
    freeze_session,
    load_validation_data,
    load_verdicts,
    other_family_params,
    panel_meta,
    params_for,
    ranking_choice,
    regime_filter_helps,
    rs_ranking,
    run_insample,
    run_oos,
)

NOW = datetime(2026, 10, 19, 21, 0, tzinfo=UTC)
_FIELDS = ("open", "high", "low", "close", "volume")


# ---------------------------------------------------------------------------- the gate


def test_gate_failure_messages_exact():
    oos = {"expectancy_r": 0.1, "profit_factor": 1.05, "n": 80, "n_distinct": 80}
    v = evaluate_gate(oos, 0.15, -0.30, -0.25, coverage=0.95)
    assert v.failures == [
        "profit factor 1.05 < 1.20",
        "only 80 distinct trades (need 100)",
        "expectancy 0.10R not above random entry 0.15R",
        "drawdown 30% worse than SPY 25%",
    ]
    assert v.passed is False


def test_low_coverage_raises_profit_factor_floor():
    oos = {"expectancy_r": 0.3, "profit_factor": 1.25, "n": 200, "n_distinct": 200}
    assert "profit factor 1.25 < 1.30" in evaluate_gate(oos, 0.0, -0.1, -0.2, 0.80).failures
    assert evaluate_gate(oos, 0.0, -0.1, -0.2, 0.90) == Verdict(passed=True, failures=[])


def test_gate_fails_a_non_positive_expectancy_first():
    oos = {"expectancy_r": -0.04, "profit_factor": 0.9, "n": 150, "n_distinct": 150}
    v = evaluate_gate(oos, -0.2, -0.1, -0.2, 1.0)
    assert v.failures == ["expectancy -0.04R ≤ 0", "profit factor 0.90 < 1.20"]


def test_gate_drawdown_equal_to_spy_is_not_worse():
    oos = {"expectancy_r": 0.2, "profit_factor": 1.5, "n": 100, "n_distinct": 100}
    assert evaluate_gate(oos, 0.1, -0.25, -0.25, 1.0).passed
    assert evaluate_gate(oos, 0.1, -0.26, -0.25, 1.0).failures == [
        "drawdown 26% worse than SPY 25%"
    ]


def test_an_unknown_drawdown_fails_the_gate_and_an_infinite_profit_factor_passes():
    nan = float("nan")
    oos = {"expectancy_r": 0.3, "profit_factor": float("inf"), "n": 200, "n_distinct": 200}
    assert evaluate_gate(oos, 0.1, -0.1, -0.2, 1.0).passed  # no losing trade at all
    assert evaluate_gate(oos, 0.1, nan, -0.2, 1.0).failures == ["drawdown unknown"]
    assert evaluate_gate(oos, 0.1, -0.1, nan, 1.0).failures == ["drawdown unknown"]


def test_gate_with_no_trades_fails_every_trade_condition():
    nan = float("nan")
    oos = {"expectancy_r": nan, "profit_factor": nan, "n": 0, "n_distinct": 0}
    v = evaluate_gate(oos, nan, 0.0, -0.1, 1.0)
    assert not v.passed
    assert v.failures[2] == "only 0 distinct trades (need 100)" and len(v.failures) == 4


def test_the_trade_floor_counts_distinct_trades_not_every_triggered_idea():
    # 300 triggered ideas, but a stock near its high re-triggers daily: 60 could be taken.
    oos = {"expectancy_r": 0.3, "profit_factor": 1.6, "n": 300, "n_distinct": 60}
    assert evaluate_gate(oos, 0.1, -0.1, -0.2, 1.0).failures == [
        "only 60 distinct trades (need 100)"
    ]
    assert evaluate_gate({**oos, "n_distinct": 100}, 0.1, -0.1, -0.2, 1.0).passed


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
        protocol=protocol_block(family),
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


def test_a_missing_git_is_a_prereg_error(tmp_git_repo, monkeypatch):
    path = _prereg_path(tmp_git_repo, "breakout", "breakout-v1")
    write_draft(path, _prereg())
    _commit(tmp_git_repo, path)
    monkeypatch.setenv("PATH", str(tmp_git_repo / "no-such-bin"))  # git can't be found
    with pytest.raises(PreregError) as err:
        check_committed(path, tmp_git_repo)
    assert str(err.value) == "git is required to verify pre-registration"


def _old_style_prereg_file(path: Path, **over) -> Prereg:
    """A pre-registration as written before the protocol block existed (no ``protocol`` key)."""
    prereg = _prereg(protocol=None, **over)
    doc = dataclasses.asdict(prereg)
    del doc["protocol"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return prereg


def _no_data_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a run gets as far as loading data (the refusal must come first)."""

    def refuse(*args, **kwargs):
        raise AssertionError("the data was read before the run was refused")

    monkeypatch.setattr(run_module, "load_validation_data", refuse)


def test_the_protocol_block_seals_every_rule_outside_the_setup_params():
    block = protocol_block("breakout")
    assert block == {
        "leader": dataclasses.asdict(LeaderParams()),
        "universe": dataclasses.asdict(UniverseFilter()),
        "execution": dataclasses.asdict(ExecutionParams()),
        "risk": dataclasses.asdict(RiskSettings()),
        "regime_factor": dict(REGIME_FACTOR),
        "grid": GRIDS["breakout"],
    }
    assert json.loads(json.dumps(block)) == block  # it reads back from the file unchanged
    assert protocol_block("pullback")["grid"] == GRIDS["pullback"]


def test_an_old_prereg_without_a_protocol_block_still_reads_and_verifies_its_seal(tmp_path):
    path = _prereg_path(tmp_path, "breakout", "breakout-v1")
    written = _old_style_prereg_file(path)
    loaded = load_prereg(path)
    assert loaded.protocol is None and loaded.is_sealed() and loaded.sha256 == written.sha256


def test_oos_refuses_a_prereg_without_a_protocol_block(tmp_git_repo, monkeypatch):
    _no_data_read(monkeypatch)
    path = _prereg_path(tmp_git_repo, "breakout", "breakout-v1")
    _old_style_prereg_file(path)
    _commit(tmp_git_repo, path)
    with pytest.raises(PreregError) as err:
        run_oos("breakout", "breakout-v1", tmp_git_repo, NOW)
    assert str(err.value) == (
        "breakout-v1.prereg.json has no protocol block (it was written before the rules outside "
        "the setup parameters were sealed): re-run insample to draft a new pre-registration"
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda mp: mp.setitem(REGIME_FACTOR, "mixed", 0.75),
        lambda mp: mp.setitem(GRIDS, "breakout", {"stop_atr": [2.0], "trail_atr": [3.0]}),
    ],
)
def test_oos_refuses_a_prereg_sealed_under_rules_the_code_no_longer_has(
    tmp_git_repo, monkeypatch, change
):
    _no_data_read(monkeypatch)
    path = _prereg_path(tmp_git_repo, "breakout", "breakout-v1")
    write_draft(path, _prereg())
    _commit(tmp_git_repo, path)
    change(monkeypatch)  # config.py edited after the pre-registration was drafted
    with pytest.raises(PreregError) as err:
        run_oos("breakout", "breakout-v1", tmp_git_repo, NOW)
    assert str(err.value) == (
        "breakout-v1.prereg.json was registered under other rules (leader gate, universe, "
        "execution, risk, regime factor or grid) than the code has now: re-run insample"
    )


# ---------------------------------------------------------------------------- reports on disk


def _register(repo: Path, family: str = "breakout", n: int = 1, **params) -> str:
    """Write a sealed pre-registration for ``<family>-v<n>`` and return its sha256."""
    version = f"{family}-v{n}"
    defaults = {"breakout": BreakoutParams, "pullback": PullbackParams}[family]
    prereg = _prereg(
        family, version, params=dataclasses.asdict(defaults(version=version, **params))
    )
    write_draft(_prereg_path(repo, family, version), prereg)
    return prereg.sha256


def _write_report(
    repo: Path,
    family: str,
    n: int,
    *,
    passed: bool,
    freeze: str = "2026-09-30",
    helps: bool = True,
    rs_helps: bool = True,
    prereg_sha256: str | None = None,
) -> Path:
    path = repo / "ml" / "validation" / family / f"{family}-v{n}.report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "family": family,
        "version": f"{family}-v{n}",
        "prereg_sha256": prereg_sha256,
        "run_index": n,
        "freeze_date": freeze,
        "regime_filter": {"helps": helps},
        "ranking": {"rs_helps": rs_helps},
        "verdict": {"passed": passed, "failures": [] if passed else ["only 80 trades (need 100)"]},
    }
    path.write_text(json.dumps(report))
    return path


def _live(repo: Path, family: str = "breakout", **report) -> None:
    """A passing v1 report whose pre-registration is on file and unchanged."""
    _write_report(repo, family, 1, passed=True, prereg_sha256=_register(repo, family), **report)


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


def test_the_version_must_be_the_next_run(tmp_path):
    with pytest.raises(ValueError, match="is breakout-v1, not breakout-v2"):
        run_oos("breakout", "breakout-v2", tmp_path, NOW)  # nothing has run: v1 is next
    _write_report(tmp_path, "breakout", 1, passed=False)
    with pytest.raises(ValueError, match="is breakout-v2, not breakout-v3"):
        run_oos("breakout", "breakout-v3", tmp_path, NOW)


def test_load_verdicts_defaults_to_not_yet_tested(tmp_path):
    untested = {"live": False, "version": None, "failures": ["not yet tested"], "report_path": None}
    assert load_verdicts(tmp_path)["breakout"] == untested
    assert list(load_verdicts(tmp_path)) == ["breakout", "pullback"]


def test_load_verdicts_reads_the_latest_run_of_each_family(tmp_path):
    _write_report(tmp_path, "breakout", 1, passed=True, prereg_sha256=_register(tmp_path))
    _write_report(tmp_path, "breakout", 2, passed=False)
    _live(tmp_path, "pullback")
    verdicts = load_verdicts(tmp_path)
    assert verdicts["breakout"] == {
        "version": "breakout-v2",
        "live": False,
        "failures": ["only 80 trades (need 100)"],
        "report_path": "ml/validation/breakout/breakout-v2.report.json",
    }
    assert verdicts["pullback"]["live"] is True and verdicts["pullback"]["failures"] == []


def test_live_needs_the_sealed_prereg_that_the_passing_run_used(tmp_path):
    changed = "pre-registration missing or changed since its run"
    sha = _register(tmp_path, stop_atr=2.5)
    _write_report(tmp_path, "breakout", 1, passed=True, prereg_sha256=sha)
    assert load_verdicts(tmp_path)["breakout"]["live"] is True
    assert default_setups(tmp_path)[0].live and default_setups(tmp_path)[0].params.stop_atr == 2.5

    _register(tmp_path, stop_atr=1.5)  # re-sealed after the run: a valid file, not the tested one
    verdict = load_verdicts(tmp_path)["breakout"]
    assert (verdict["live"], verdict["failures"]) == (False, [changed])
    assert verdict["version"] == "breakout-v1"
    assert not default_setups(tmp_path)[0].live

    path = _prereg_path(tmp_path, "breakout", "breakout-v1")
    doc = json.loads(path.read_text())
    doc["params"]["stop_atr"], doc["sha256"] = 2.5, sha  # hand-edited back, seal not redone
    doc["created_at"] = "2026-10-20T00:00:00+00:00"
    path.write_text(json.dumps(doc))
    assert load_verdicts(tmp_path)["breakout"]["failures"] == [changed]
    spec = default_setups(tmp_path)[0]
    assert not spec.live and spec.params == BreakoutParams()  # an unsealed file is never used

    path.unlink()  # e.g. a clone with the report but not the pre-registration
    assert load_verdicts(tmp_path)["breakout"]["failures"] == [changed]
    spec = default_setups(tmp_path)[0]
    assert not spec.live and spec.params == BreakoutParams()  # research-only ideas keep flowing


def test_live_needs_the_rules_its_run_was_registered_under(tmp_path, monkeypatch):
    """The incident: the pre-registration sealed only the setup's own parameters, so editing the
    leader gate, the universe, the fill model or the risk rules after a passing run kept the
    family Live on rules that were never tested."""
    _live(tmp_path)
    assert load_verdicts(tmp_path)["breakout"]["live"] is True
    with monkeypatch.context() as mp:
        mp.setitem(REGIME_FACTOR, "mixed", 0.75)
        verdict = load_verdicts(tmp_path)["breakout"]
        assert (verdict["live"], verdict["failures"]) == (False, [RULES_CHANGED])
        assert RULES_CHANGED == "rules changed since its run"
        spec = default_setups(tmp_path)[0]
        assert not spec.live and spec.params.version == "breakout-v1"  # research only
    assert load_verdicts(tmp_path)["breakout"]["live"] is True  # the same rules again

    old = _old_style_prereg_file(_prereg_path(tmp_path, "breakout", "breakout-v1"))
    _write_report(tmp_path, "breakout", 1, passed=True, prereg_sha256=old.sha256)
    verdict = load_verdicts(tmp_path)["breakout"]
    assert (verdict["live"], verdict["failures"]) == (False, [RULES_UNSEALED])
    assert RULES_UNSEALED.startswith(RULES_CHANGED) and "no protocol block" in RULES_UNSEALED


@pytest.mark.parametrize(
    "text",
    ["{not json", json.dumps([1, 2]), json.dumps({"version": "breakout-v2", "run_index": "2"})],
)
def test_an_unreadable_report_makes_the_family_research_only_and_never_breaks_the_scan(
    tmp_path, text
):
    _live(tmp_path)
    (tmp_path / "ml/validation/breakout/breakout-v2.report.json").write_text(text)

    assert load_verdicts(tmp_path)["breakout"] == {
        "version": None,
        "live": False,
        "failures": ["report unreadable"],
        "report_path": "ml/validation/breakout/breakout-v2.report.json",
    }
    assert load_verdicts(tmp_path)["pullback"]["failures"] == ["not yet tested"]
    spec = default_setups(tmp_path)[0]
    assert (spec.live, spec.params, spec.regime_gate) == (False, BreakoutParams(), True)
    assert ranking_choice(tmp_path) == "rs"
    # Counting the budget needs every report, so an out-of-sample or in-sample run stops.
    with pytest.raises(ValueError, match=r"breakout-v2\.report\.json"):
        run_oos("breakout", "breakout-v3", tmp_path, NOW)
    with pytest.raises(ValueError, match=r"breakout-v2\.report\.json"):
        run_insample("breakout", tmp_path)


def test_params_for_reads_the_prereg_or_falls_back_to_defaults(tmp_path):
    assert params_for("breakout", None, tmp_path) == BreakoutParams()
    assert params_for("pullback", "pullback-v2", tmp_path) == PullbackParams()  # no file
    _register(tmp_path, stop_atr=2.5, trail_atr=3.5)
    assert params_for("breakout", "breakout-v1", tmp_path) == BreakoutParams(
        stop_atr=2.5, trail_atr=3.5
    )
    _prereg_path(tmp_path, "breakout", "breakout-v1").write_text("{broken")
    assert params_for("breakout", "breakout-v1", tmp_path) == BreakoutParams()


def test_default_setups_use_preregistered_params_and_verdict(tmp_path):
    sha = _register(tmp_path, stop_atr=2.5)
    _write_report(tmp_path, "breakout", 1, passed=True, prereg_sha256=sha)

    specs = default_setups(tmp_path)
    spec = {s.params.version: s for s in specs}["breakout-v1"]
    assert spec.live and spec.params.stop_atr == 2.5 and spec.regime_gate
    assert [type(s.params) for s in specs] == [BreakoutParams, PullbackParams]
    assert specs[1].params == PullbackParams() and not specs[1].live and specs[1].regime_gate


def test_the_combined_portfolio_takes_the_other_family_only_from_its_own_report(tmp_path):
    none_yet = (None, "other family not yet validated")
    assert other_family_params(tmp_path, "breakout") == none_yet
    sha = _register(tmp_path, "pullback", trail_atr=3.5)
    assert other_family_params(tmp_path, "breakout") == none_yet  # registered, never run

    _write_report(tmp_path, "pullback", 1, passed=False, prereg_sha256=sha)
    params, note = other_family_params(tmp_path, "breakout")
    assert params == PullbackParams(version="pullback-v1", trail_atr=3.5)
    assert note == "with pullback-v1 (its pre-registered parameters)"

    _register(tmp_path, "pullback", trail_atr=2.5)  # re-sealed: not the file its run used
    assert other_family_params(tmp_path, "breakout") == (
        None,
        "other family's pre-registration missing or changed since its run",
    )


def test_regime_filter_that_does_not_help_turns_into_warning(tmp_path):
    _live(tmp_path, helps=False)
    spec = {s.params.version: s for s in default_setups(tmp_path)}["breakout-v1"]
    assert spec.live and spec.regime_gate is False


def test_ranking_choice_falls_back_to_proximity_when_rs_does_not_help(tmp_path):
    assert ranking_choice(tmp_path) == "rs"  # nothing validated yet
    _write_report(tmp_path, "pullback", 1, passed=False, rs_helps=False)
    assert ranking_choice(tmp_path) == "rs"  # only a Live family's ranking counts
    _live(tmp_path, rs_helps=False)
    assert ranking_choice(tmp_path) == "proximity"


def test_the_regime_filter_is_dropped_only_on_strong_evidence():
    """The incident: with few downtrend sessions since 2023 the old "helps" test (expectancy and
    drawdown, either one) was close to a coin flip, and a False sized mixed and downtrend ideas at
    full risk under a Live label. Now the gate stays unless all three conditions hold."""
    nan = float("nan")
    passes, fails = Verdict(passed=True, failures=[]), Verdict(passed=False, failures=["x"])
    assert not regime_filter_helps(30, 0.05, 0.08, passes)  # enough evidence, all three hold
    assert regime_filter_helps(29, 0.05, 0.08, passes)  # too few downtrend trades
    assert regime_filter_helps(30, 0.08, 0.08, passes)  # ungated Sharpe not higher (a tie)
    assert regime_filter_helps(30, 0.09, 0.08, passes)  # the gated book had the better Sharpe
    assert regime_filter_helps(30, 0.05, 0.08, fails)  # trading every regime fails the gate
    assert regime_filter_helps(30, nan, 0.08, passes)  # unmeasurable: keep the gate
    assert regime_filter_helps(30, 0.05, nan, passes)


def _closed_trades(rs: np.ndarray, r: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({"status": "closed", "rs_pct": rs, "r_multiple": r})


def test_rs_ranking_compares_the_top_quintile_with_the_bottom_one():
    rs = np.arange(70.0, 100.0)  # 30 distinct values: 6 trades per quintile
    rising = rs_ranking(_closed_trades(rs, (rs - 85.0) / 10.0))
    q = rising["quintile_expectancy"]
    assert len(q) == 5 and q == sorted(q) and rising["rs_helps"] is True
    assert q[0] == pytest.approx((np.arange(70.0, 76.0) - 85.0).mean() / 10.0)

    assert rs_ranking(_closed_trades(rs, (85.0 - rs) / 10.0))["rs_helps"] is False
    dip = np.where((rs >= 82) & (rs < 88), -5.0, (rs - 85.0) / 10.0)  # middle quintile is awful
    assert rs_ranking(_closed_trades(rs, dip))["rs_helps"] is True  # only q5 vs q1 counts

    still_open = _closed_trades(rs, (85.0 - rs) / 10.0).assign(status="open")
    assert rs_ranking(still_open) == {"quintile_expectancy": [], "rs_helps": True}
    one_group = rs_ranking(_closed_trades(np.full(10, 80.0), np.linspace(-1, 1, 10)))
    assert one_group["rs_helps"] is True  # nothing to compare


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


async def test_load_validation_data_reads_the_store_in_symbol_batches(monkeypatch):
    """The loader reads only the columns a panel needs, a batch of symbols at a time, and pivots
    each batch before the next is read, so the peak is one batch of rows, not the whole market."""
    panel = _shifted(synthetic_universe_panel(seed=2, n_symbols=3, n_sessions=300), "2016-01-04")
    calls: dict[str, list] = {"prefilter": [], "ohlcv": [], "raw": [], "log": []}
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

    async def load_ohlcv(symbols, start, end) -> pd.DataFrame:
        calls["ohlcv"].append((list(symbols), start, end))
        return _long_bars(panel, symbols, start, end, raw=False)

    async def load_raw_closes(symbols, start, end) -> pd.DataFrame:
        calls["raw"].append((list(symbols), start, end))
        return _long_bars(panel, symbols, start, end, raw=True)[["ts", "symbol", "close"]]

    async def load_assets() -> pd.DataFrame:
        return assets

    async def last_bar_dates(resolution: str = "1d") -> dict[str, date]:
        return {"DEAD1": date(2018, 3, 1), "S00": date(2017, 2, 1), "SPY": date(2017, 2, 1)}

    for name, fake in [
        ("liquid_prefilter", liquid_prefilter),
        ("load_ohlcv", load_ohlcv),
        ("load_raw_closes", load_raw_closes),
        ("load_assets", load_assets),
        ("last_bar_dates", last_bar_dates),
    ]:
        monkeypatch.setattr(store, name, fake)

    class _Log:
        def info(self, event: str, **kw) -> None:
            calls["log"].append((event, kw))

    monkeypatch.setattr(run_module, "log", _Log())
    monkeypatch.setattr(run_module, "LOAD_BATCH", 2)
    data = await load_validation_data()

    assert calls["prefilter"] == [UniverseFilter().min_median_dollar_volume]
    batches = [["S00", "S01"], ["S02", "SPY"]]
    assert [c[0] for c in calls["ohlcv"]] == batches == [c[0] for c in calls["raw"]]
    for _, start, end in calls["ohlcv"] + calls["raw"]:
        assert start == IS_START and panel.dates()[-1].date() <= end <= datetime.now(UTC).date()
    same = {"check_like": True, "check_freq": False}  # columns come back sorted
    pd.testing.assert_frame_equal(data.panel.close, panel.close, **same)
    pd.testing.assert_frame_equal(data.panel.volume, panel.volume, **same)
    assert data.panel.raw_close is not None
    pd.testing.assert_frame_equal(data.panel.raw_close, panel.close * 2.0, **same)
    assert data.kinds["SPY"] is AssetKind.ETF and data.kinds["S01"] is AssetKind.COMMON
    assert data.delisted == frozenset({"DEAD1", "DEAD2", "WARR"})
    assert data.coverage == 0.5  # DEAD1 of DEAD1/DEAD2; the warrant is not common stock
    assert data.meta == panel_meta(data.panel)
    loaded = dict(calls["log"])["validation.data_loaded"]
    assert loaded == {
        "symbols": 4, "batches": 2, "rows": 4 * 300, "raw_rows": 4 * 300, "sessions": 300,
    }  # fmt: skip


async def test_the_prefilter_threshold_is_the_universe_filter_s(monkeypatch):
    seen: list[float] = []

    async def liquid_prefilter(min_dollar_volume: float) -> list[str]:
        seen.append(min_dollar_volume)
        return []

    async def no_rows(symbols, start, end) -> pd.DataFrame:
        return pd.DataFrame(columns=["ts", "symbol", "open", "high", "low", "close", "volume"])

    monkeypatch.setattr(store, "liquid_prefilter", liquid_prefilter)
    monkeypatch.setattr(store, "load_ohlcv", no_rows)
    monkeypatch.setattr(store, "load_raw_closes", no_rows)
    with pytest.raises(ValueError, match="backfill"):
        await load_validation_data(UniverseFilter(min_median_dollar_volume=5_000_000.0))
    assert seen == [5_000_000.0]


# ---------------------------------------------------------------------------- end to end


def _validation_panel(seed: int = 3) -> Panel:
    """40 stocks plus SPY from mid-2020 to mid-2023: 2.5 years in sample, 7 months out of it."""
    return _shifted(synthetic_universe_panel(seed=seed, n_symbols=40, n_sessions=800), "2020-06-01")


REPORT_KEYS = {
    "family", "version", "prereg_sha256", "run_index", "n_trials", "freeze_date", "oos_window",
    "is_stats", "grid", "oos_trades", "oos_trades_distinct", "oos_trades_regime_off", "by_year",
    "baseline", "portfolio_alone", "portfolio_combined", "portfolio_combined_note", "spy",
    "coverage", "fingerprint", "deflated_sharpe", "verdict", "regime_filter", "ranking",
    "code_commit", "code_dirty",
}  # fmt: skip


class _ContextCounter:
    """Counts market-context builds, wherever the harness or the trade code makes them."""

    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        self.n = 0
        real = run_module.build_context

        def counting(*args, **kwargs):
            self.n += 1
            return real(*args, **kwargs)

        mp.setattr(run_module, "build_context", counting)
        mp.setattr(trades_module, "build_context", counting)

    def take(self) -> int:
        n, self.n = self.n, 0
        return n


@pytest.fixture(scope="module")
def validated(tmp_path_factory) -> dict:
    """``run_insample`` then ``run_oos`` for breakout in a fresh git repo, run once per module."""
    repo = _init_repo(tmp_path_factory.mktemp("e2e"))
    panel = _validation_panel()
    data = _data(panel, delisted=frozenset({"S07"}))

    with pytest.MonkeyPatch.context() as mp:
        contexts = _ContextCounter(mp)
        summary = run_insample("breakout", repo, data=data)
        built = {"insample": contexts.take()}
        prereg = Path(summary["prereg_path"])
        with pytest.raises(PreregError, match="not committed"):
            run_oos("breakout", summary["version"], repo, NOW, data=data)
        _commit(repo, prereg)
        report = run_oos("breakout", summary["version"], repo, NOW, data=data)
        built["oos"] = contexts.take()
    return {"repo": repo, "data": data, "summary": summary, "report": report, "built": built}


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
    assert prereg["protocol"] == protocol_block("breakout")


def test_insample_records_what_the_checkpoint_has_to_report(validated):
    """The in-sample record is what the human checkpoint reads before any out-of-sample run: the
    delisted coverage, the data fingerprint, the universe size, distinct trades per variant and
    the chosen variant's year-by-year results."""
    repo, data, summary = validated["repo"], validated["data"], validated["summary"]
    record = json.loads((repo / "ml/validation/breakout/breakout-v1.insample.json").read_text())
    assert record["coverage"] == 0.95 == summary["coverage"]
    assert record["fingerprint"] == data_fingerprint(data.meta)

    panel = data.panel
    cut = panel.dates()[panel.dates() <= pd.Timestamp(IS_END)][-1]
    ctx = build_context(panel, data.kinds, cut.date())
    sizes = ctx.eligible.loc[pd.Timestamp(IS_START) :].sum(axis=1)
    assert record["universe"] == {"median_eligible": float(sizes.median()), "sessions": len(sizes)}
    assert record["universe"]["median_eligible"] > 0
    assert all(0 < row["n_distinct"] <= row["n"] for row in record["grid"])

    params = dataclasses.replace(BreakoutParams(), **record["chosen"]["params"])
    is_panel = truncate(panel, cut)
    found = candidate_history(is_panel, data.kinds, params, IS_START, cut.date())
    trades = simulate_candidates(
        is_panel, compute_indicators(is_panel), found, params, ExecutionParams(), frozenset()
    )
    expected = json_safe(by_year(regime_on(trades)), infinity_as_str=True)
    assert record["chosen_by_year"] == expected and len(expected) >= 1
    assert sum(y["n"] for y in expected.values()) == record["chosen"]["n"]


def test_the_insample_printout_shows_the_checkpoint_numbers(validated):
    summary = validated["summary"]
    out = cli.format_insample(summary)
    fp = summary["fingerprint"]
    assert f"{fp['symbols']} symbols" in out and "delisted coverage 95%" in out
    assert f"median {summary['universe']['median_eligible']:,.0f} eligible" in out
    assert "Distinct" in out and "by year" in out
    for year in summary["chosen_by_year"]:
        assert str(year) in out


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
    on = report["oos_trades"]
    assert on["n_distinct"] == report["oos_trades_distinct"]["n"] <= on["n"]
    regime = report["regime_filter"]
    assert set(regime) == {
        "helps", "on", "off", "max_dd", "sharpe", "downtrend_trades", "ungated_gate"
    }  # fmt: skip
    assert regime["on"] == report["oos_trades"]
    assert regime["off"] == report["oos_trades_regime_off"]
    assert regime["max_dd"]["on"] == report["portfolio_alone"]["max_dd"]
    assert regime["downtrend_trades"] == regime["off"]["n"] - regime["on"]["n"]
    ungated = evaluate_gate(
        {
            k: _stored(regime["off"][k])
            for k in ("n", "n_distinct", "expectancy_r", "profit_factor")
        },
        _stored(report["baseline"]["median"]),
        _stored(regime["max_dd"]["off"]),
        _stored(report["spy"]["max_dd"]),
        _stored(report["coverage"]),
    )
    assert regime["ungated_gate"] == {"passed": ungated.passed, "failures": ungated.failures}
    assert regime["helps"] == regime_filter_helps(
        regime["downtrend_trades"],
        _stored(regime["sharpe"]["on"]),
        _stored(regime["sharpe"]["off"]),
        ungated,
    )
    quintiles = report["ranking"]["quintile_expectancy"]
    assert 1 <= len(quintiles) <= 5
    assert report["ranking"]["rs_helps"] == (len(quintiles) < 2 or quintiles[-1] > quintiles[0])
    chosen = select_variant(report["grid"])
    assert report["is_stats"] == {k: v for k, v in chosen.items() if k != "params"}
    for year in report["by_year"]:
        assert int(year) >= OOS_START.year

    # Pullback has no out-of-sample report yet: nothing of its window may reach this report.
    assert report["portfolio_combined"] is None
    assert report["portfolio_combined_note"] == "other family not yet validated"
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    assert report["code_commit"] == head.stdout.strip() and report["code_dirty"] is False

    for name in ("portfolio_alone", "spy"):
        weekly = report[name]["equity_weekly"]
        assert weekly[0][0] >= OOS_START.isoformat() and weekly[-1][0] >= freeze
        assert all(pd.Timestamp(d).day_name() == "Friday" for d, _ in weekly)
    assert report["spy"]["equity_weekly"][0][1] == pytest.approx(1.0, abs=0.05)
    assert report["portfolio_alone"]["max_dd"] <= 0.0


def _stored(value) -> float:
    """A number as the report stores it: null is NaN, "inf"/"-inf" are infinities."""
    return math.nan if value is None else float(value)


def _verdict_from_stored_numbers(report: dict) -> dict:
    v = evaluate_gate(
        {
            k: _stored(report["oos_trades"][k])
            for k in ("n", "n_distinct", "expectancy_r", "profit_factor")
        },
        _stored(report["baseline"]["median"]),
        _stored(report["portfolio_alone"]["max_dd"]),
        _stored(report["spy"]["max_dd"]),
        _stored(report["coverage"]),
    )
    return {"passed": v.passed, "failures": v.failures}


def test_oos_verdict_is_what_the_gate_says_about_the_reported_numbers(validated):
    report = validated["report"]
    assert report["verdict"] == _verdict_from_stored_numbers(report)


def test_one_context_per_window_gives_the_numbers_rebuilding_it_per_call_gave(validated):
    assert validated["built"] == {"insample": 1, "oos": 2}  # IS cut; IS cut + freeze
    repo, data, report = validated["repo"], validated["data"], validated["report"]
    panel, ex = data.panel, ExecutionParams()

    cut = panel.dates()[panel.dates() <= pd.Timestamp(IS_END)][-1]
    is_panel = truncate(panel, cut)
    is_ind = compute_indicators(is_panel)
    rows = []
    for combo in grid_variants("breakout"):
        params = dataclasses.replace(BreakoutParams(), **combo)
        found = candidate_history(is_panel, data.kinds, params, IS_START, cut.date())
        found = simulate_candidates(is_panel, is_ind, found, params, ex, frozenset())
        rows.append({"params": combo, **trade_stats(regime_on(found))})
    assert json_safe(rows, infinity_as_str=True) == report["grid"]

    freeze = panel.dates()[-1]
    params = params_for("breakout", "breakout-v1", repo)
    found = candidate_history(panel, data.kinds, params, OOS_START, freeze.date())
    found = simulate_candidates(panel, compute_indicators(panel), found, params, ex, data.delisted)
    on = regime_on(found)
    assert json_safe(trade_stats(on), infinity_as_str=True) == report["oos_trades"]
    distinct = json_safe(trade_stats(distinct_trades(on)), infinity_as_str=True)
    assert distinct == report["oos_trades_distinct"]


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
    plain_repo = _init_repo(tmp_path / "plain")
    prereg = Path(run_insample("pullback", plain_repo, data=_data(panel))["prereg_path"])
    reports = []
    for name, p in (("plain", panel), ("scrambled", scrambled)):
        repo = _init_repo(tmp_path / name)
        path = _prereg_path(repo, "pullback", "pullback-v1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prereg.read_text())  # one pre-registration, the same in both repos
        _commit(repo, path)
        reports.append(run_oos("pullback", "pullback-v1", repo, NOW, data=_data(p)))
    assert reports[0]["freeze_date"] == freeze.date().isoformat()
    # Two repos: their commits (recorded as the code version) may differ, nothing else may.
    same = [{k: v for k, v in r.items() if k != "code_commit"} for r in reports]
    assert same[0] == same[1]


def test_oos_refuses_parameters_that_are_not_the_in_sample_choice(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    path = _prereg_path(repo, "breakout", "breakout-v1")
    write_draft(path, _prereg(params=dataclasses.asdict(BreakoutParams(stop_atr=3.7))))  # off-grid
    _commit(repo, path)
    with pytest.raises(PreregError) as err:
        run_oos("breakout", "breakout-v1", repo, NOW, data=_data(_validation_panel()))
    assert str(err.value) == "breakout-v1 is not the in-sample choice; re-run insample"
    assert not (repo / "ml/validation/breakout/breakout-v1.report.json").exists()


def test_a_revision_identical_to_an_earlier_version_is_refused(tmp_path, monkeypatch):
    """The incident: a "revision" could re-run v1's exact rules, so the same setup got up to three
    looks at overlapping out-of-sample windows."""
    repo = _init_repo(tmp_path / "repo")
    data = _data(_validation_panel())
    _commit(repo, Path(run_insample("pullback", repo, data=data)["prereg_path"]))
    _write_report(repo, "pullback", 1, passed=False)  # v1 ran and failed

    with pytest.raises(PreregError) as err:
        run_insample("pullback", repo, data=data, hypothesis="same again, maybe it passes")
    message = "pullback-v2 is identical to pullback-v1; a revision must change the rules"
    assert str(err.value) == message
    assert not list((repo / "ml/validation/pullback").glob("pullback-v2.*"))

    # A hand-made v2 with v1's rules is refused out of sample too, before any data is read.
    v1 = load_prereg(_prereg_path(repo, "pullback", "pullback-v1"))
    fields = {**dataclasses.asdict(v1), "version": "pullback-v2", "hypothesis": "same again"}
    fields["params"] = {**v1.params, "version": "pullback-v2"}
    del fields["sha256"]
    path = _prereg_path(repo, "pullback", "pullback-v2")
    write_draft(path, Prereg.create(**fields))
    _commit(repo, path)
    with monkeypatch.context() as mp:
        _no_data_read(mp)
        with pytest.raises(PreregError) as err:
            run_oos("pullback", "pullback-v2", repo, NOW)
    assert str(err.value) == message

    # Declaring a different grid changes the rules, so that revision may go ahead.
    monkeypatch.setitem(GRIDS, "pullback", {"trail_atr": [2.5, 3.0], "stop_buffer_atr": [0.25]})
    summary = run_insample("pullback", repo, data=data, hypothesis="a narrower grid")
    assert summary["version"] == "pullback-v2"


def test_the_freeze_needs_a_fully_loaded_last_session():
    panel = _validation_panel()
    last, before = panel.dates()[-1], panel.dates()[-2]
    assert freeze_session(panel) == last.date()
    stocks = [s for s in panel.symbols() if s != "SPY"]
    panel.close.loc[last, stocks[:2]] = np.nan  # 39 of 41: within 95% of the session before
    assert freeze_session(panel) == last.date()
    panel.close.loc[last, stocks[:5]] = np.nan  # 36 of 41: an update stopped half way
    with pytest.raises(ValueError) as err:
        freeze_session(panel)
    assert str(err.value) == (
        f"the data looks incomplete on {last.date()}: 36 symbols have a bar, against 41 on "
        f"{before.date()}; run `python -m swing_engine backfill --refresh-assets && "
        "python -m swing_engine update`, then retry"
    )


def test_oos_refuses_to_freeze_on_a_half_loaded_session(tmp_git_repo):
    panel = _validation_panel()
    stocks = [s for s in panel.symbols() if s != "SPY"]
    panel.close.loc[panel.dates()[-1], stocks[:10]] = np.nan
    path = _prereg_path(tmp_git_repo, "breakout", "breakout-v1")
    write_draft(path, _prereg())
    _commit(tmp_git_repo, path)
    with pytest.raises(ValueError, match="backfill --refresh-assets"):
        run_oos("breakout", "breakout-v1", tmp_git_repo, NOW, data=_data(panel))
    assert not (tmp_git_repo / "ml/validation/breakout/breakout-v1.report.json").exists()


def test_deflated_sharpe_uses_the_sample_skew_and_kurtosis_of_the_daily_returns(validated):
    repo, data, report = validated["repo"], validated["data"], validated["report"]
    panel = data.panel
    freeze = panel.dates()[-1].date()
    params = params_for("breakout", "breakout-v1", repo)
    found = candidate_history(panel, data.kinds, params, OOS_START, freeze)
    trades = simulate_candidates(
        panel, compute_indicators(panel), found, params, ExecutionParams(), data.delisted
    )
    alone = simulate_portfolio(trades, panel.close, RiskSettings(), OOS_START, freeze)
    equity = alone.equity.to_numpy(dtype="float64")
    returns = equity[1:] / equity[:-1] - 1.0

    dsr = report["deflated_sharpe"]
    assert dsr["skew"] == pytest.approx(float(sps.skew(returns)))
    assert dsr["kurtosis"] == pytest.approx(float(sps.kurtosis(returns, fisher=False)))
    assert abs(dsr["skew"]) > 1e-6 and abs(dsr["kurtosis"] - 3.0) > 1e-6  # not the normal default
    expected = deflated_sharpe(
        dsr["observed_sharpe"], dsr["n_trials"], dsr["n_obs"], dsr["skew"], dsr["kurtosis"]
    )
    assert _stored(dsr["value"]) == pytest.approx(expected, nan_ok=True)


def test_the_combined_portfolio_uses_the_other_family_s_registered_parameters(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path / "repo")
    data = _data(_validation_panel())
    sha = _register(repo, "pullback", trail_atr=3.5, stop_buffer_atr=0.5)  # off the defaults
    _write_report(repo, "pullback", 1, passed=False, prereg_sha256=sha)  # pullback-v1 has run
    _commit(repo, Path(run_insample("breakout", repo, data=data)["prereg_path"]))

    seen: list = []
    real = run_module.context_candidates

    def spy(ctx, params, start):
        seen.append(params)
        return real(ctx, params, start)

    monkeypatch.setattr(run_module, "context_candidates", spy)
    report = run_oos("breakout", "breakout-v1", repo, NOW, data=data)
    assert PullbackParams(version="pullback-v1", trail_atr=3.5, stop_buffer_atr=0.5) in seen
    assert report["portfolio_combined"] is not None
    assert report["portfolio_combined"]["trades"] > 0
    assert report["portfolio_combined_note"] == "with pullback-v1 (its pre-registered parameters)"


def test_an_infinite_profit_factor_is_stored_so_the_verdict_can_be_rechecked(tmp_path, monkeypatch):
    real = run_module.trade_stats

    def no_losers(trades: pd.DataFrame) -> dict:
        stats = real(trades)
        return {**stats, "profit_factor": math.inf} if stats["n"] else stats

    monkeypatch.setattr(run_module, "trade_stats", no_losers)
    repo = _init_repo(tmp_path / "repo")
    data = _data(_validation_panel())
    _commit(repo, Path(run_insample("pullback", repo, data=data)["prereg_path"]))
    report = run_oos("pullback", "pullback-v1", repo, NOW, data=data)

    stored = json.loads((repo / "ml/validation/pullback/pullback-v1.report.json").read_text())
    assert stored["oos_trades"]["profit_factor"] == "inf"
    assert stored["grid"][0]["profit_factor"] == "inf"
    assert not any("profit factor" in f for f in stored["verdict"]["failures"])
    assert stored["verdict"] == _verdict_from_stored_numbers(stored) == report["verdict"]
    assert "profit factor inf" in cli.format_oos(stored)


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
            "oos_trades": {"n": 80, "n_distinct": 44, "expectancy_r": 0.1, "profit_factor": 1.05},
            "baseline": {"median": 0.15}, "portfolio_alone": {"max_dd": -0.30},
            "spy": {"max_dd": -0.25}, "deflated_sharpe": {"value": 0.61}, "coverage": 0.93,
            "verdict": {"passed": False, "failures": ["only 44 distinct trades (need 100)"]},
        }  # fmt: skip

    monkeypatch.setattr(cli, "run_oos", fake)
    out = _main("oos", "--family", "pullback", "--version", "pullback-v1")

    assert (seen["family"], seen["version"]) == ("pullback", "pullback-v1")
    assert seen["repo"] == Path.cwd()
    assert seen["now"].tzinfo is not None
    assert "FAILED" in out and "Research only" in out and "only 44 distinct trades" in out
    assert "80 closed ideas, 44 distinct trades" in out
    assert "pullback-v1.report.json" in out


def test_the_printouts_survive_missing_and_infinite_numbers():
    report = {
        "family": "breakout", "version": "breakout-v1", "oos_window": ["2023-01-01", "2026-10-16"],
        "oos_trades": {"n": 0, "expectancy_r": None, "profit_factor": "inf"},
        "baseline": {"median": None}, "portfolio_alone": {"max_dd": None},
        "spy": {"max_dd": -0.25}, "deflated_sharpe": {"value": None}, "coverage": None,
        "verdict": {"passed": False, "failures": ["drawdown unknown"]},
    }  # fmt: skip
    out = cli.format_oos(report)
    assert "expectancy n/a" in out and "profit factor inf" in out and "max drawdown n/a" in out
    assert "0 closed ideas, n/a distinct trades" in out  # a report from before n_distinct
    assert "SPY 25%" in out and "deflated Sharpe n/a" in out and "drawdown unknown" in out

    row = {"n": 120, "win_rate": None, "expectancy_r": 0.2, "profit_factor": "inf", "avg_hold": 9.0}
    grid = [{"params": {"stop_atr": 1.5, "trail_atr": 2.5}, **row}]
    summary = {"family": "breakout", "version": "breakout-v1", "grid": grid, "chosen": grid[0],
               "prereg_path": "x.prereg.json"}  # fmt: skip
    assert "inf" in cli.format_insample(summary) and "n/a" in cli.format_insample(summary)


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
