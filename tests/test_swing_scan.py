"""Swing engine: the nightly scan and ``python -m swing_engine scan``.

The scan composes everything built before it: the point-in-time universe, SPY's regime, relative
strength, the leader gate, both setups and fixed-fractional sizing. These tests run it on a
synthetic universe of 40 trending stocks plus SPY (``synthetic_universe_panel``) and pin:

* a scan for session ``d`` cannot see anything after ``d`` (the future is scrambled and the
  answer must not move, with at least one idea so the comparison is not vacuous);
* the regime gate (a downtrend moves gated ideas to ``watch_only`` at zero shares, a mixed market
  halves the risk budget, an ungated setup stays in ``ideas`` at full size with a warning);
* ranking, overlaps with open positions, capacity, and the JSON-safe dict Phase 2 will serve;
* the CLI's argument parsing, load window and printed table, against a faked store, because the
  real one needs Postgres.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import threading
from contextlib import asynccontextmanager, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import swing_engine.__main__ as cli
from swing_engine import store
from swing_engine.config import BreakoutParams, PullbackParams, RiskSettings, UniverseFilter
from swing_engine.data.assets import AssetKind
from swing_engine.panel import Panel
from swing_engine.regime import Regime, market_regime
from swing_engine.scan import (
    Idea,
    ScanContext,
    ScanError,
    ScanResult,
    SetupSpec,
    build_context,
    default_setups,
    run_scan,
    scan_to_dict,
)
from swing_engine.setups import candidates_for
from swing_engine.sizing import Capacity, Sizing, size_idea
from swing_engine.testing import scramble_after, synthetic_kinds, synthetic_universe_panel
from swing_engine.validation.prereg import Prereg, write_draft
from swing_engine.validation.protocol import protocol_block

# SPY + 40 stocks, 600 sessions. Seed 5 gives session 450 three breakout and three pullback
# candidates whose RS order and proximity order differ, which the ranking tests need.
PANEL = synthetic_universe_panel(seed=5)
KINDS = synthetic_kinds(PANEL)
D = PANEL.dates()[450]
AS_OF = D.date()
SETTINGS = RiskSettings()
_FIELDS = ("open", "high", "low", "close", "volume")


@pytest.fixture(autouse=True)
def _no_validation_reports(tmp_path, monkeypatch):
    """Run from an empty directory: ``default_setups()`` and the scan CLI read the verdicts
    under the current directory, and these tests are about unvalidated default setups."""
    monkeypatch.chdir(tmp_path)


def _scan(panel: Panel = PANEL, **kw) -> ScanResult:
    kw.setdefault("setups", default_setups())
    kw.setdefault("kinds", KINDS)
    return run_scan(
        panel,
        kw.pop("kinds"),
        kw.pop("as_of", AS_OF),
        kw.pop("settings", SETTINGS),
        kw.pop("setups"),
        **kw,
    )


def _spy_path(kind: str, n: int) -> np.ndarray:
    """A SPY close path whose regime at session 450 is known (asserted where it is used)."""
    t = np.arange(n)
    noise = 0.004 * np.random.default_rng(3).standard_normal(n)
    log_return = {
        "down": np.full(n, -0.002),
        # a long advance, then a crash from session 420: close under SMA200, SMA50 still above
        "mixed": np.where(t < 420, 0.002, -0.015),
    }[kind]
    return 100.0 * np.exp(np.cumsum(log_return + noise))


def _with_spy(panel: Panel, kind: str) -> Panel:
    """A copy of ``panel`` whose SPY series follows ``kind`` (every price field moves together)."""
    path = _spy_path(kind, len(panel.dates()))
    frames = {f: getattr(panel, f).copy() for f in _FIELDS}
    for f in ("open", "high", "low", "close"):
        frames[f]["SPY"] = path
    return Panel(**frames, raw_close=None if panel.raw_close is None else frames["close"].copy())


def _regime_at_d(panel: Panel) -> Regime:
    return Regime(market_regime(panel.close["SPY"]).loc[D])


def _clone(panel: Panel, source: str, name: str, scale: float = 1.0) -> Panel:
    """``panel`` plus a column ``name`` that is ``source`` scaled: identical daily returns."""
    frames = {}
    for f in (*_FIELDS, "raw_close"):
        frame = getattr(panel, f).copy()
        frame[name] = frame[source] * (1.0 if f == "volume" else scale)
        frames[f] = frame
    return Panel(**frames)


# ---------------------------------------------------------------------------- look-ahead


def test_scan_is_lookahead_free():
    a = _scan()
    assert a.ideas, "premise: the scan date has ideas, so the comparison below says something"
    future_scrambled = scramble_after(PANEL, D, seed=9)
    assert not future_scrambled.close.equals(PANEL.close)  # the future really was changed
    b = _scan(future_scrambled)
    assert scan_to_dict(a) == scan_to_dict(b)


def test_build_context_ends_at_the_scan_session_and_computes_from_it():
    ctx = build_context(PANEL, KINDS, AS_OF)

    assert isinstance(ctx, ScanContext) and ctx.session == D
    for frame in (ctx.panel.close, ctx.eligible, ctx.rs_pct, ctx.leaders, ctx.ind.sma50):
        assert frame.index[-1] == D and len(frame) == 451
    assert ctx.regime.index[-1] == D
    assert ctx.kinds is KINDS
    assert ctx.leaders.loc[D].any() and ctx.eligible.loc[D].any()


def test_build_context_honours_the_universe_filter():
    strict = build_context(PANEL, KINDS, AS_OF, universe=UniverseFilter(min_price=1e9))
    assert not strict.eligible.to_numpy().any() and not strict.leaders.to_numpy().any()
    assert _scan(universe=UniverseFilter(min_price=1e9)).ideas == []


# ---------------------------------------------------------------------------- a prebuilt context


def test_a_prebuilt_context_gives_the_same_scan_as_building_one():
    """The nightly job builds one context per session and reuses it for the scan, tracking,
    positions and the API cache; handing it to ``run_scan`` must not change a single number."""
    ctx = build_context(PANEL, KINDS, AS_OF)
    held = [("S01", 240.0)]
    rebuilt = _scan(open_positions=held, rank_by="proximity")
    reused = _scan(open_positions=held, rank_by="proximity", ctx=ctx)
    assert rebuilt.ideas, "premise: the scan has ideas, so the comparison says something"
    assert scan_to_dict(reused) == scan_to_dict(rebuilt)


def test_a_prebuilt_context_is_used_instead_of_rebuilding(monkeypatch):
    ctx = build_context(PANEL, KINDS, AS_OF)

    def no_rebuild(*args, **kwargs):
        raise AssertionError("run_scan rebuilt the context it was given")

    monkeypatch.setattr("swing_engine.scan.build_context", no_rebuild)
    assert scan_to_dict(_scan(ctx=ctx))["session"] == AS_OF.isoformat()


def test_a_prebuilt_context_for_another_session_is_refused():
    ctx = build_context(PANEL, KINDS, PANEL.dates()[449].date())
    with pytest.raises(ValueError, match="context"):
        _scan(ctx=ctx)


# ---------------------------------------------------------------------------- the result


def test_result_fields_describe_the_session():
    r = _scan()
    ctx = build_context(PANEL, KINDS, AS_OF)

    assert type(r.session) is date and r.session == AS_OF
    assert r.regime is Regime.UPTREND and r.regime_factor == 1.0  # premise of most tests below
    assert r.universe_size == int(ctx.eligible.loc[D].sum()) and r.universe_size > 0
    assert 0.0 <= r.breadth <= 100.0 and type(r.breadth) is float
    assert r.watch_only == []


def test_ideas_are_exactly_the_candidates_dated_the_scan_day():
    r = _scan()
    ctx = build_context(PANEL, KINDS, AS_OF)
    expected = set()
    for spec in default_setups():
        c = candidates_for(ctx.panel, ctx.ind, ctx.leaders, ctx.rs_pct, spec.params)
        expected |= {(s, spec.params.version) for s in c.loc[c["date"] == D, "symbol"]}

    assert {(i.symbol, i.version) for i in r.ideas} == expected
    assert len(r.ideas) == len(expected) > 0  # the full list: the top-N cut is display-only


def test_an_idea_carries_its_levels_and_python_floats():
    r = _scan()
    ctx = build_context(PANEL, KINDS, AS_OF)
    for idea in r.ideas:
        assert idea.setup in {"breakout", "pullback"} and idea.version.endswith("-v1")
        assert idea.stop < idea.trigger and idea.atr14 > 0
        assert idea.rs_pct == pytest.approx(ctx.rs_pct.at[D, idea.symbol])
        assert idea.sizing == size_idea(idea.trigger, idea.stop, SETTINGS, 1.0)
        assert all(type(x) is float for x in (idea.trigger, idea.stop, idea.atr14, idea.rs_pct))
        assert type(idea.rank) is int and idea.overlaps == []
        assert idea.live is False and idea.regime_warning is False


def test_live_comes_from_the_spec():
    both = [SetupSpec(BreakoutParams(), live=True), SetupSpec(PullbackParams(), live=False)]
    r = _scan(setups=both)
    assert {i.setup for i in r.ideas} == {"breakout", "pullback"}  # premise: both families
    assert all(i.live == (i.setup == "breakout") for i in r.ideas)


def test_spy_is_never_an_idea_even_when_its_kind_is_wrong():
    wrong = {**KINDS, "SPY": AssetKind.COMMON}
    r = _scan(kinds=wrong)
    assert "SPY" not in {i.symbol for i in r.ideas}
    assert r.universe_size == _scan().universe_size


# ---------------------------------------------------------------------------- regime gating


def test_downtrend_moves_ideas_to_watch_only_with_zero_shares():
    uptrend = _scan()
    down = _with_spy(PANEL, "down")
    assert _regime_at_d(down) is Regime.DOWNTREND  # premise

    r = _scan(down)

    assert r.regime is Regime.DOWNTREND and r.regime_factor == 0.0
    assert r.ideas == []
    assert {(i.symbol, i.setup) for i in r.watch_only} == {
        (i.symbol, i.setup) for i in uptrend.ideas
    }
    assert all(i.sizing.shares == 0 and i.sizing.risk_dollars == 0.0 for i in r.watch_only)
    assert all(i.sizing.capped_by is None for i in r.watch_only)  # a regime, not a bad stop
    assert all(i.regime_warning is False for i in r.watch_only)  # gated: they're parked, not sized


def test_mixed_regime_halves_the_risk_budget():
    mixed = _with_spy(PANEL, "mixed")
    assert _regime_at_d(mixed) is Regime.MIXED  # premise

    r = _scan(mixed)

    assert r.regime is Regime.MIXED and r.regime_factor == 0.5
    assert r.ideas and r.watch_only == []
    for idea in r.ideas:
        assert idea.sizing == size_idea(idea.trigger, idea.stop, SETTINGS, 0.5)
        assert idea.regime_warning is False


def test_ungated_setup_keeps_ideas_in_downtrend_with_regime_warning():
    down = _with_spy(PANEL, "down")
    ungated = SetupSpec(PullbackParams(), live=False, regime_gate=False)
    gated = SetupSpec(BreakoutParams(), live=False)

    r = _scan(down, setups=[ungated, gated])

    assert r.regime is Regime.DOWNTREND and r.regime_factor == 0.0  # the market's factor
    assert r.ideas and r.watch_only, "premise: both families have candidates tonight"
    for idea in r.ideas:
        assert idea.setup == "pullback" and idea.regime_warning is True
        assert idea.sizing == size_idea(idea.trigger, idea.stop, SETTINGS, 1.0)
        assert idea.sizing.shares > 0
    assert all(i.setup == "breakout" and i.sizing.shares == 0 for i in r.watch_only)


def test_ungated_setup_in_a_mixed_market_is_full_size_and_warned():
    mixed = _with_spy(PANEL, "mixed")
    r = _scan(mixed, setups=[SetupSpec(PullbackParams(), live=False, regime_gate=False)])

    assert r.ideas
    assert all(i.regime_warning for i in r.ideas)
    assert all(i.sizing == size_idea(i.trigger, i.stop, SETTINGS, 1.0) for i in r.ideas)


def test_ungated_setup_in_an_uptrend_has_no_warning():
    r = _scan(setups=[SetupSpec(PullbackParams(), live=False, regime_gate=False)])
    assert r.ideas and not any(i.regime_warning for i in r.ideas)


# ---------------------------------------------------------------------------- ranking


def test_ideas_ranked_by_rs_desc_then_symbol():
    r = _scan()
    assert len(r.ideas) >= 3
    assert [i.rank for i in r.ideas] == list(range(1, len(r.ideas) + 1))
    keys = [(-i.rs_pct, i.symbol, i.setup) for i in r.ideas]
    assert keys == sorted(keys)


def test_equal_rs_breaks_ties_by_symbol_then_version():
    # AAA_TWIN is an exact copy of a pullback candidate, so the two tie on RS and every level.
    # Two pullback specs make each of them appear twice; the spec order must not leak into the
    # ranking: both rows of the first symbol come before both rows of the second.
    v1, v2 = PullbackParams(), PullbackParams(version="pullback-v2")
    specs = [SetupSpec(v1, live=False), SetupSpec(v2, live=False)]
    symbol = _scan(setups=specs).ideas[0].symbol
    r = _scan(
        _clone(PANEL, symbol, "AAA_TWIN"),
        kinds={**KINDS, "AAA_TWIN": AssetKind.COMMON},
        setups=specs,
    )

    tied = [i for i in r.ideas if i.symbol in {symbol, "AAA_TWIN"}]
    assert [(i.symbol, i.version) for i in tied] == [
        ("AAA_TWIN", "pullback-v1"),
        ("AAA_TWIN", "pullback-v2"),
        (symbol, "pullback-v1"),
        (symbol, "pullback-v2"),
    ]
    assert len({i.rs_pct for i in tied}) == 1
    assert [i.rank for i in tied] == list(range(tied[0].rank, tied[0].rank + 4))


def test_a_stock_that_is_a_candidate_for_two_specs_appears_once_per_spec():
    v2 = PullbackParams(version="pullback-v2")
    r = _scan(setups=[SetupSpec(PullbackParams(), live=False), SetupSpec(v2, live=False)])

    by_symbol: dict[str, list[str]] = {}
    for i in r.ideas:
        by_symbol.setdefault(i.symbol, []).append(i.version)
    assert by_symbol and all(
        sorted(v) == ["pullback-v1", "pullback-v2"] for v in by_symbol.values()
    )
    assert [i.rank for i in r.ideas] == list(range(1, len(r.ideas) + 1))  # one combined ranking


def test_proximity_ranking_orders_by_close_over_trigger():
    by_rs = _scan()
    r = _scan(rank_by="proximity")

    close = PANEL.close.loc[D]
    keys = [(-close[i.symbol] / i.trigger, i.symbol, i.setup) for i in r.ideas]
    assert keys == sorted(keys)
    assert [i.rank for i in r.ideas] == list(range(1, len(r.ideas) + 1))
    assert [i.symbol for i in r.ideas] != [i.symbol for i in by_rs.ideas]  # a different order
    assert {(i.symbol, i.setup) for i in r.ideas} == {(i.symbol, i.setup) for i in by_rs.ideas}


def test_watch_only_is_ranked_on_its_own_from_one():
    r = _scan(_with_spy(PANEL, "down"))
    assert r.watch_only
    assert [i.rank for i in r.watch_only] == list(range(1, len(r.watch_only) + 1))
    keys = [(-i.rs_pct, i.symbol, i.setup) for i in r.watch_only]
    assert keys == sorted(keys)


def test_an_unknown_ranking_is_rejected():
    with pytest.raises(ValueError, match="rank_by"):
        _scan(rank_by="alphabetical")


# ---------------------------------------------------------------------------- SPY is required


def test_missing_spy_bar_raises_scan_error():
    frames = {f: getattr(PANEL, f).copy() for f in _FIELDS}
    for frame in frames.values():
        frame.loc[D, "SPY"] = np.nan
    no_bar = Panel(**frames)

    with pytest.raises(ScanError, match=str(AS_OF)):
        _scan(no_bar)
    with pytest.raises(ScanError, match=str(AS_OF)):
        build_context(no_bar, KINDS, AS_OF)


def test_a_panel_without_spy_raises_scan_error():
    stocks = Panel(**{f: getattr(PANEL, f).drop(columns="SPY") for f in _FIELDS})
    with pytest.raises(ScanError, match="SPY"):
        _scan(stocks)


def test_a_date_with_no_session_raises_scan_error():
    saturday = D + pd.Timedelta(days=(5 - D.weekday()) % 7 or 7)
    assert saturday.weekday() == 5
    with pytest.raises(ScanError, match=str(saturday.date())):
        _scan(as_of=saturday.date())


def test_a_date_before_the_panel_raises_scan_error():
    with pytest.raises(ScanError, match="2000-01-03"):
        _scan(as_of=date(2000, 1, 3))


# ---------------------------------------------------------------------------- the book


def test_capacity_reflects_open_positions():
    r = _scan(open_positions=[("MSFT", 240.0)])
    assert r.capacity.open_positions == 1 and r.capacity.max_risk_dollars == pytest.approx(1500.0)
    assert r.capacity == Capacity(
        open_positions=1, max_positions=8, open_risk_dollars=240.0, max_risk_dollars=1500.0
    )


def test_capacity_with_a_flat_book():
    assert _scan().capacity == Capacity(0, 8, 0.0, 1500.0)


def test_capacity_follows_the_settings():
    settings = RiskSettings(equity=50_000.0, max_positions=3, max_total_risk_pct=0.04)
    r = _scan(settings=settings, open_positions=[("AAA", 100.0), ("BBB", 50.5)])
    assert r.capacity == Capacity(2, 3, 150.5, 2000.0)
    assert all(i.sizing == size_idea(i.trigger, i.stop, settings, 1.0) for i in r.ideas)


def test_ideas_flag_open_positions_they_move_with():
    symbol = _scan().ideas[0].symbol
    held = _clone(PANEL, symbol, "HELD", scale=1.7)  # same returns as `symbol`: corr 1.0
    kinds = {**KINDS, "HELD": AssetKind.COMMON}

    r = _scan(held, kinds=kinds, open_positions=[("HELD", 120.0)])

    flagged = {i.symbol: i.overlaps for i in r.ideas}
    assert flagged[symbol] == ["HELD"]
    assert all(o == [] for s, o in flagged.items() if s not in {symbol, "HELD"})
    assert flagged.get("HELD", []) == []  # a holding is never compared with itself


def test_a_flat_book_flags_no_overlap():
    assert all(i.overlaps == [] for i in _scan().ideas)


# ---------------------------------------------------------------------------- defaults


def test_default_setups_are_both_families_unproven_and_gated():
    specs = default_setups()
    assert [type(s.params) for s in specs] == [BreakoutParams, PullbackParams]
    assert specs[0].params == BreakoutParams() and specs[1].params == PullbackParams()
    assert all(s.live is False and s.regime_gate is True for s in specs)
    assert default_setups(Path.cwd()) == specs


def test_a_spec_is_frozen_and_gated_by_default():
    spec = SetupSpec(BreakoutParams(), live=True)
    assert spec.regime_gate is True
    with pytest.raises(AttributeError):
        spec.live = False  # type: ignore[misc]


# ---------------------------------------------------------------------------- JSON-safe dict


def test_scan_to_dict_is_json_safe_and_round_trips():
    r = _scan(open_positions=[("MSFT", 240.0)])
    d = scan_to_dict(r)

    text = json.dumps(d, allow_nan=False)  # allow_nan=False: NaN/Infinity would raise
    assert json.loads(text) == d
    assert d["session"] == AS_OF.isoformat() and d["regime"] == "uptrend"
    assert d["regime_factor"] == 1.0 and d["universe_size"] == r.universe_size
    assert d["capacity"] == {
        "open_positions": 1,
        "max_positions": 8,
        "open_risk_dollars": 240.0,
        "max_risk_dollars": 1500.0,
    }
    first = d["ideas"][0]
    assert set(first) == {
        "symbol", "setup", "version", "trigger", "stop", "atr14", "rs_pct", "rank",
        "sizing", "overlaps", "live", "regime_warning",
    }  # fmt: skip
    assert set(first["sizing"]) == {"shares", "risk_dollars", "position_value", "capped_by"}
    assert first["rank"] == 1 and first["live"] is False and first["overlaps"] == []
    assert d["watch_only"] == []


def test_scan_to_dict_turns_nan_into_none_and_enums_into_values():
    idea_sizing = Sizing(shares=0, risk_dollars=0.0, position_value=0.0, capped_by="no_risk")
    idea = Idea(
        symbol="AAA", setup="breakout", version="breakout-v1", trigger=np.float64(10.5),
        stop=9.5, atr14=0.5, rs_pct=float("nan"), rank=1, sizing=idea_sizing,
        overlaps=["BBB"], live=False, regime_warning=True,
    )  # fmt: skip
    r = ScanResult(
        session=date(2026, 10, 16), regime=Regime.MIXED, regime_factor=0.5,
        breadth=float("nan"), universe_size=0, ideas=[idea], watch_only=[],
        capacity=Capacity(0, 8, 0.0, 1500.0),
    )  # fmt: skip

    d = scan_to_dict(r)

    assert d["breadth"] is None and d["ideas"][0]["rs_pct"] is None
    assert d["regime"] == "mixed" and d["session"] == "2026-10-16"
    assert type(d["ideas"][0]["trigger"]) is float and d["ideas"][0]["overlaps"] == ["BBB"]
    json.dumps(d, allow_nan=False)


# ---------------------------------------------------------------------------- test helpers


def test_scramble_after_changes_only_the_future():
    scrambled = scramble_after(PANEL, D, seed=4)

    for f in (*_FIELDS, "raw_close"):
        before, after = getattr(PANEL, f), getattr(scrambled, f)
        assert after.index.equals(before.index) and list(after.columns) == list(before.columns)
        assert after.loc[:D].equals(before.loc[:D])
        assert not after.loc[D + pd.Timedelta(days=1) :].isna().to_numpy().any()
        assert not np.allclose(after.loc[D + pd.Timedelta(days=1) :], before.loc[D:].iloc[1:])
    assert scramble_after(PANEL, D, seed=4).close.equals(scrambled.close)  # seeded
    assert not scramble_after(PANEL, D, seed=5).close.equals(scrambled.close)


def test_synthetic_universe_shape_and_kinds():
    assert "SPY" in PANEL.symbols()
    assert len(PANEL.symbols()) == 41 and len(PANEL.dates()) == 600
    assert PANEL.raw_close is not None and PANEL.close.index.tz is None
    assert KINDS["SPY"] is AssetKind.ETF
    assert {k for s, k in KINDS.items() if s != "SPY"} == {AssetKind.COMMON}
    assert set(KINDS) == set(PANEL.symbols())
    small = synthetic_universe_panel(seed=2, n_symbols=3, n_sessions=300)
    assert len(small.symbols()) == 4 and len(small.dates()) == 300
    assert synthetic_universe_panel(seed=5).close.equals(PANEL.close)  # deterministic
    assert not synthetic_universe_panel(seed=6).close.equals(PANEL.close)


# ---------------------------------------------------------------------------- the CLI


def _long_bars(panel: Panel, symbols: list[str], start: date, end: date, raw: bool) -> pd.DataFrame:
    """The ``store.load_daily_bars`` shape for ``panel``'s rows between two session dates."""
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    rows = []
    for symbol in symbols:
        if symbol not in panel.close.columns:
            continue
        frame = pd.DataFrame({f: getattr(panel, f)[symbol] for f in _FIELDS}).loc[lo:hi].dropna()
        if raw and panel.raw_close is not None:
            frame["close"] = panel.raw_close.loc[frame.index, symbol]
        frame.insert(0, "symbol", symbol)
        frame.insert(0, "ts", frame.index.tz_localize("UTC"))
        rows.append(frame.reset_index(drop=True))
    if not rows:
        return pd.DataFrame(columns=["ts", "symbol", *_FIELDS])
    return pd.concat(rows, ignore_index=True)


class _FakeStore:
    """Replaces the three store reads the scan command makes and records how it called them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, panel: Panel = PANEL) -> None:
        self.bar_calls: list[tuple[list[str], date, date, str]] = []
        self.prefilter_calls: list[float] = []
        self.events: list[str] = []
        outer = self
        monkeypatch.setattr(cli, "load_env", lambda: outer.events.append("load_env"))
        monkeypatch.setattr(cli, "setup_logging", lambda: outer.events.append("setup_logging"))

        kinds = synthetic_kinds(panel)

        async def load_assets() -> pd.DataFrame:
            return pd.DataFrame({"symbol": list(kinds), "kind": [k.value for k in kinds.values()]})

        async def liquid_prefilter(min_dollar_volume: float) -> list[str]:
            outer.prefilter_calls.append(min_dollar_volume)
            return [s for s in panel.symbols() if s != "SPY"]

        async def last_bar_dates(resolution: str = "1d") -> dict[str, date]:
            return {s: panel.dates()[-1].date() for s in panel.symbols()}

        async def load_daily_bars(symbols, start, end, resolution="1d") -> pd.DataFrame:
            outer.bar_calls.append((list(symbols), start, end, resolution))
            return _long_bars(panel, list(symbols), start, end, raw=resolution == "1d_raw")

        monkeypatch.setattr(store, "load_assets", load_assets)
        monkeypatch.setattr(store, "liquid_prefilter", liquid_prefilter)
        monkeypatch.setattr(store, "last_bar_dates", last_bar_dates)
        monkeypatch.setattr(store, "load_daily_bars", load_daily_bars)


def _run_cli(*argv: str) -> str:
    with redirect_stdout(io.StringIO()) as out:
        cli.main(["scan", *argv])
    return out.getvalue()


def test_help_lists_scan_and_the_data_jobs(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert exit_.value.code == 0 and "scan" in out and "backfill" in out and "update" in out


def test_scan_arguments_default_and_parse():
    parser = cli.build_parser()
    assert parser.parse_args(["scan"]).as_of is None
    given = parser.parse_args(["scan", "--as-of", "2026-10-16"])
    assert (given.cmd, given.as_of) == ("scan", date(2026, 10, 16))


def test_a_malformed_as_of_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["scan", "--as-of", "last-friday"])
    assert exit_.value.code == 2 and "last-friday" in capsys.readouterr().err


def test_scan_command_loads_the_window_and_prints_the_regime_and_table(monkeypatch):
    fake = _FakeStore(monkeypatch)
    out = _run_cli("--as-of", AS_OF.isoformat())

    assert fake.events == ["load_env", "setup_logging"]
    assert fake.prefilter_calls == [20_000_000]
    assert len(fake.bar_calls) == 2
    (adj_symbols, start, end, adj_res), (raw_symbols, raw_start, raw_end, raw_res) = fake.bar_calls
    assert (adj_res, raw_res) == ("1d", "1d_raw")
    assert adj_symbols == raw_symbols == sorted(PANEL.symbols())  # the prefilter's, plus SPY
    assert (start, end) == (AS_OF - timedelta(days=800), AS_OF) == (raw_start, raw_end)

    first = out.splitlines()[0]
    assert AS_OF.isoformat() in first and "UPTREND" in first and "breadth" in first.lower()
    expected = _scan()
    assert f"{expected.universe_size}" in first
    for header in ("Symbol", "Setup", "Buy above", "Stop", "Shares", "$ risk", "RS"):
        assert header in out
    top = expected.ideas[0]
    row = next(line for line in out.splitlines() if top.symbol in line.split())
    assert f"{top.trigger:.2f}" in row and f"{top.stop:.2f}" in row
    assert str(top.sizing.shares) in row.split() and top.setup in row.split()
    assert "UNPROVEN" in out  # default setups are not live


def test_scan_command_defaults_to_the_latest_spy_session(monkeypatch):
    fake = _FakeStore(monkeypatch)
    out = _run_cli()
    assert fake.bar_calls[0][2] == PANEL.dates()[-1].date()
    assert PANEL.dates()[-1].date().isoformat() in out.splitlines()[0]


def test_scan_command_lists_the_ideas_run_scan_ranks_in_the_same_order(monkeypatch):
    _FakeStore(monkeypatch)
    out = _run_cli("--as-of", AS_OF.isoformat())
    rows = [
        line.split() for line in out.splitlines() if line.split()[:1] and line.split()[0].isdigit()
    ]
    assert [(r[1], r[2]) for r in rows] == [(i.symbol, i.setup) for i in _scan().ideas]


def test_scan_command_ranks_by_proximity_when_a_live_setup_found_rs_did_not_help(
    monkeypatch, tmp_path
):
    _FakeStore(monkeypatch)
    folder = tmp_path / "ml" / "validation" / "breakout"
    prereg = Prereg.create(
        family="breakout", version="breakout-v1", params=dataclasses.asdict(BreakoutParams()),
        gate={}, is_window=[], oos_start="2023-01-01", hypothesis="", created_at="2026-10-19",
        protocol=protocol_block("breakout"),  # Live needs the rules it was registered under
    )  # fmt: skip
    write_draft(folder / "breakout-v1.prereg.json", prereg)
    report = {
        "version": "breakout-v1", "run_index": 1, "freeze_date": "2026-09-30",
        "prereg_sha256": prereg.sha256, "verdict": {"passed": True, "failures": []},
        "regime_filter": {"helps": True}, "ranking": {"rs_helps": False},
    }  # fmt: skip
    (folder / "breakout-v1.report.json").write_text(json.dumps(report))
    out = _run_cli("--as-of", AS_OF.isoformat())

    rows = [w for w in map(str.split, out.splitlines()) if w and w[0].isdigit()]
    by_proximity = [(i.symbol, i.setup) for i in _scan(rank_by="proximity").ideas]
    assert by_proximity != [(i.symbol, i.setup) for i in _scan().ideas]  # premise
    assert [(r[1], r[2]) for r in rows] == by_proximity


def test_the_scan_loader_is_public_and_the_cli_uses_it():
    """One data path: the CLI's scan and the nightly job load bars through the same function."""
    from swing_engine import loader

    assert cli.load_scan_inputs is loader.load_scan_inputs
    assert not hasattr(cli, "_load_scan_inputs")


async def test_the_loader_returns_the_panel_kinds_and_session(monkeypatch):
    fake = _FakeStore(monkeypatch)
    from swing_engine.loader import load_scan_inputs

    panel, kinds, session = await load_scan_inputs(AS_OF, UniverseFilter())
    assert session == AS_OF and panel.dates()[-1] == D and kinds == KINDS
    expected = PANEL.close.loc[:D]
    assert list(panel.dates()) == list(expected.index)
    assert np.allclose(panel.close[expected.columns].to_numpy(), expected.to_numpy())
    assert fake.prefilter_calls == [UniverseFilter().min_median_dollar_volume]


async def test_the_loader_always_loads_extra_symbols_below_the_prefilter(monkeypatch):
    """The nightly job passes the symbols the user holds: a position in a stock that fell below
    the liquidity prefilter still needs its bars for tonight's action."""
    _FakeStore(monkeypatch)
    from swing_engine.loader import load_scan_inputs

    async def thin_prefilter(min_dollar_volume: float) -> list[str]:
        return ["S00", "S01"]

    monkeypatch.setattr(store, "liquid_prefilter", thin_prefilter)
    plain, _, _ = await load_scan_inputs(AS_OF, UniverseFilter())
    held, _, _ = await load_scan_inputs(AS_OF, UniverseFilter(), extra_symbols=["S07"])
    assert sorted(plain.symbols()) == ["S00", "S01", "SPY"]
    assert sorted(held.symbols()) == ["S00", "S01", "S07", "SPY"]


def test_scan_prefilter_threshold_is_the_universe_filter_s(monkeypatch):
    """The prefilter is only a cheap necessary condition for the universe's dollar-volume floor,
    so it must use that floor: a hard-coded copy would silently drop names if the floor fell."""
    fake = _FakeStore(monkeypatch)
    loose = UniverseFilter(min_median_dollar_volume=5_000_000.0)
    monkeypatch.setattr(cli, "UniverseFilter", lambda: loose)
    _run_cli("--as-of", AS_OF.isoformat())
    assert fake.prefilter_calls == [5_000_000.0]


def test_scan_command_shows_only_the_top_20_and_says_how_many_more(monkeypatch):
    _FakeStore(monkeypatch)
    # Four versions of each family: every candidate shows up four times, 24 ideas in all.
    specs = [
        SetupSpec(params(version=f"{family}-v{n}"), live=False)
        for family, params in (("breakout", BreakoutParams), ("pullback", PullbackParams))
        for n in range(1, 5)
    ]
    monkeypatch.setattr(cli, "default_setups", lambda repo=None: specs)
    full = _scan(setups=specs)
    assert len(full.ideas) > 20, "premise: more ideas than the display cut"

    out = _run_cli("--as-of", AS_OF.isoformat())

    ranks = [int(w[0]) for w in map(str.split, out.splitlines()) if w and w[0].isdigit()]
    assert ranks == list(range(1, 21))
    assert f"{len(full.ideas) - 20} more" in out


def test_scan_command_lists_watch_only_ideas_in_a_downtrend(monkeypatch):
    down = _with_spy(PANEL, "down")
    _FakeStore(monkeypatch, down)

    out = _run_cli("--as-of", AS_OF.isoformat())

    assert "DOWNTREND" in out.splitlines()[0]
    assert "watch only" in out.lower()
    assert out.lower().index("watch only") < out.index(_scan(down).watch_only[0].symbol)
    assert "No ideas" in out  # nothing is sized tonight


def test_scan_command_without_spy_exits_1_with_a_clear_message(monkeypatch):
    stocks = Panel(**{f: getattr(PANEL, f).drop(columns="SPY") for f in _FIELDS})
    _FakeStore(monkeypatch, stocks)

    with pytest.raises(SystemExit) as exit_:
        _run_cli("--as-of", AS_OF.isoformat())

    assert str(exit_.value.code).startswith("error:") and "SPY" in str(exit_.value.code)


def test_scan_command_with_no_bars_exits_1_and_says_to_backfill(monkeypatch):
    _FakeStore(monkeypatch, PANEL)

    with pytest.raises(SystemExit) as exit_:
        _run_cli("--as-of", "2010-01-04")  # before any stored bar

    assert str(exit_.value.code).startswith("error:") and "backfill" in str(exit_.value.code)


# ---------------------------------------------------------------------------- the printout


def _idea(rank: int, symbol: str, **over) -> Idea:
    fields = dict(
        symbol=symbol, setup="breakout", version="breakout-v1", trigger=148.2, stop=140.1,
        atr14=4.05, rs_pct=97.4, rank=rank, sizing=size_idea(148.2, 140.1, SETTINGS, 1.0),
        overlaps=[], live=False, regime_warning=False,
    )  # fmt: skip
    return Idea(**{**fields, **over})


def _result(**over) -> ScanResult:
    fields = dict(
        session=date(2026, 10, 16), regime=Regime.UPTREND, regime_factor=1.0, breadth=61.04,
        universe_size=812, ideas=[_idea(1, "NVDA"), _idea(2, "AMD", live=True)], watch_only=[],
        capacity=Capacity(1, 8, 240.0, 1500.0),
    )  # fmt: skip
    return ScanResult(**{**fields, **over})


def test_format_scan_prints_the_regime_line_then_a_row_per_idea():
    text = cli.format_scan(_result())
    lines = text.splitlines()

    assert lines[0] == (
        "2026-10-16  regime UPTREND (size factor 1.0)  breadth 61% above SMA50  universe 812 stocks"
    )
    assert "1 of 8 positions" in text and "$240 of $1,500 open risk" in text
    nvda = next(line for line in lines if " NVDA " in line).split()
    assert nvda[:8] == ["1", "NVDA", "breakout", "148.20", "140.10", "30", "243", "97"]
    assert "UNPROVEN" in nvda
    amd = next(line for line in lines if " AMD " in line)
    assert "UNPROVEN" not in amd  # live ideas carry no marker


def test_format_scan_flags_overlaps_and_regime_warnings():
    flagged = _idea(1, "AMD", live=True, overlaps=["MSFT", "AAPL"], regime_warning=True)

    text = cli.format_scan(_result(ideas=[flagged]))

    amd = next(line for line in text.splitlines() if " AMD " in line)
    assert amd.endswith("overlaps MSFT,AAPL; regime warning")


def test_format_scan_with_nan_breadth_and_no_ideas():
    text = cli.format_scan(_result(breadth=float("nan"), ideas=[]))
    assert "breadth n/a" in text and "No ideas" in text
    assert "UNPROVEN" not in text and "watch only" not in text.lower()


def test_format_scan_cuts_at_top_n_and_counts_the_rest():
    many = [_idea(n, "NVDA") for n in range(1, 26)]
    text = cli.format_scan(_result(ideas=many), top_n=20)
    assert text.count(" NVDA ") == 20 and "5 more not shown (top 20 of 25)" in text
    assert cli.format_scan(_result(ideas=many), top_n=25).count(" NVDA ") == 25
    assert cli.format_scan(_result(ideas=many)).count(" NVDA ") == 20  # the default is the top 20


def test_format_scan_watch_only_section_shows_zero_shares():
    parked = _idea(
        1, "TSLA", setup="pullback", trigger=200.0, stop=190.0, rs_pct=88.0,
        sizing=size_idea(200.0, 190.0, SETTINGS, 0.0),
    )  # fmt: skip

    text = cli.format_scan(
        _result(regime=Regime.DOWNTREND, regime_factor=0.0, ideas=[], watch_only=[parked])
    )

    head, _, tail = text.partition("Watch only")
    assert "DOWNTREND" in head and "No ideas" in head and "TSLA" not in head
    row = next(line for line in tail.splitlines() if " TSLA " in line).split()
    assert row[:8] == ["1", "TSLA", "pullback", "200.00", "190.00", "0", "0", "88"]


def _ran_off_the_loop(real, seen):
    """``real`` wrapped to note whether each call ran with an event loop running in its thread."""

    def wrapper(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:  # no running loop in this thread
            on_loop = False
        seen.append((on_loop, threading.current_thread() is threading.main_thread()))
        return real(*args, **kwargs)

    return wrapper


async def test_the_loader_builds_the_panel_off_the_event_loop(monkeypatch):
    """The pivot of ~550 sessions x thousands of symbols is seconds of pandas: not on the loop
    that serves the API while the nightly job loads its bars."""
    from swing_engine import loader

    _FakeStore(monkeypatch)
    seen: list[tuple[bool, bool]] = []
    monkeypatch.setattr(loader, "build_panel", _ran_off_the_loop(loader.build_panel, seen))

    panel, _, session = await loader.load_scan_inputs(AS_OF, UniverseFilter())

    assert session == AS_OF and panel.dates()[-1] == D
    assert seen == [(False, False)]


async def test_the_store_builds_the_bar_frame_off_the_event_loop(monkeypatch):
    """Turning a million database rows into a frame is sync pandas work after the query's await."""
    seen: list[tuple[bool, bool]] = []
    monkeypatch.setattr(store, "_bars_frame", _ran_off_the_loop(store._bars_frame, seen))

    class _Result:
        def all(self):
            return []

    class _Session:
        async def execute(self, *_args, **_kwargs):
            return _Result()

    @asynccontextmanager
    async def fake_session_scope():
        yield _Session()

    monkeypatch.setattr(store, "session_scope", fake_session_scope)

    frame = await store.load_daily_bars(["SPY"], AS_OF - timedelta(days=5), AS_OF)

    assert frame.empty and seen == [(False, False)]
