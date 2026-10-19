"""Guards that keep the retired 8-engine composite pipeline from being re-trusted by accident.

1. `load_trend_model` refuses model artifacts trained with the leaky interleaved labels.
2. The WebSocket broadcaster no longer recomputes 1-minute composite signals (and fires
   alert rules on them) unless explicitly re-enabled.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest
from feature_engine import compute_features, gbm_bars
from quant_engine import (
    FEATURE_COLS,
    TrendArtifact,
    binary_up_labels,
    train_trend,
    triple_barrier_labels,
)
from quant_engine import train as quant_train
from quant_engine.trend import LABEL_SCHEME_PER_SYMBOL_V2, TrendModel, save
from signal_service import state, stream
from sklearn.isotonic import IsotonicRegression
from xgboost import XGBClassifier

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tiny_artifact(label_scheme: str | None, version: str = "old") -> TrendArtifact:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(60, len(FEATURE_COLS)))
    y = (x[:, 0] > 0).astype(int)
    model = XGBClassifier(n_estimators=2, max_depth=2, tree_method="hist").fit(x, y)
    calibrator = IsotonicRegression(out_of_bounds="clip").fit(model.predict_proba(x)[:, 1], y)
    kwargs = {} if label_scheme is None else {"label_scheme": label_scheme}
    return TrendArtifact(
        model=model,
        calibrator=calibrator,
        feature_cols=FEATURE_COLS,
        version=version,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    """Each test starts with no model loaded, no override env var and a spy logger."""
    monkeypatch.delenv("ATLAS_ALLOW_LEGACY_MODEL", raising=False)
    monkeypatch.setattr(state, "_trend_model", None)
    monkeypatch.setattr(state, "log", MagicMock())


# --- legacy model refusal ---------------------------------------------------------


def test_legacy_artifact_refused(tmp_path):
    path = save(_tiny_artifact(None), tmp_path / "old.joblib")

    assert state.load_trend_model(path) is None
    assert state.get_trend_model() is None
    events = [c.args[0] for c in state.log.warning.call_args_list]
    assert "trend_model.legacy_refused" in events


def test_pickle_from_before_the_field_existed_is_refused(tmp_path):
    """Old joblibs were pickled without a `label_scheme` attribute at all."""
    art = _tiny_artifact(None)
    del art.__dict__["label_scheme"]
    path = save(art, tmp_path / "ancient.joblib")

    assert state.load_trend_model(path) is None


def test_artifact_labelled_per_symbol_v2_loads(tmp_path):
    path = save(_tiny_artifact(LABEL_SCHEME_PER_SYMBOL_V2, version="new"), tmp_path / "new.joblib")

    model = state.load_trend_model(path)

    assert model is not None and model.version == "new"
    assert state.get_trend_model() is model


def test_unknown_label_scheme_is_refused(tmp_path):
    path = save(_tiny_artifact("per_symbol_v3_from_the_future"), tmp_path / "x.joblib")

    assert state.load_trend_model(path) is None


def test_legacy_artifact_loads_when_explicitly_allowed(tmp_path, monkeypatch):
    path = save(_tiny_artifact(None), tmp_path / "old.joblib")
    # The env var is read when load_trend_model is called, not when the module is imported.
    monkeypatch.setenv("ATLAS_ALLOW_LEGACY_MODEL", "1")

    model = state.load_trend_model(path)

    assert model is not None and model.version == "old"
    assert state.get_trend_model() is model
    events = [c.args[0] for c in state.log.warning.call_args_list]
    assert "trend_model.legacy_refused" not in events


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_the_literal_1_allows_a_legacy_artifact(tmp_path, monkeypatch, value):
    path = save(_tiny_artifact(None), tmp_path / "old.joblib")
    monkeypatch.setenv("ATLAS_ALLOW_LEGACY_MODEL", value)

    assert state.load_trend_model(path) is None


def test_refusal_clears_a_previously_loaded_model(tmp_path):
    good = save(_tiny_artifact(LABEL_SCHEME_PER_SYMBOL_V2, version="new"), tmp_path / "new.joblib")
    bad = save(_tiny_artifact(None), tmp_path / "old.joblib")
    assert state.load_trend_model(good) is not None

    assert state.load_trend_model(bad) is None
    assert state.get_trend_model() is None


def test_missing_model_file_behaviour_is_unchanged(tmp_path):
    assert state.load_trend_model(tmp_path / "nope.joblib") is None
    events = [c.args[0] for c in state.log.warning.call_args_list]
    assert events == ["trend_model.missing"]


def test_train_trend_without_label_scheme_is_refused_by_the_loader(tmp_path):
    """train_trend can't check how its labels were built, so by default it doesn't vouch for them."""
    feats = compute_features(gbm_bars(n=2500, seed=3))
    labels = binary_up_labels(triple_barrier_labels(feats["close"], feats["atr14"]))

    art = train_trend(feats, labels, version="unstamped", n_splits=4, test_size=250)

    assert art.label_scheme is None
    assert state.load_trend_model(save(art, tmp_path / "unstamped.joblib")) is None


def _stub_postgres_frame(n: int = 1000) -> pd.DataFrame:
    frames = [
        compute_features(gbm_bars(n=n, seed=21, s0=8.0, symbol="AAA")),
        compute_features(gbm_bars(n=n, seed=22, s0=600.0, symbol="BBB")),
    ]
    return pd.concat(frames, ignore_index=True).sort_values(["ts", "symbol"]).reset_index(drop=True)


@pytest.mark.parametrize("source", ["synthetic", "postgres"])
def test_artifact_trained_by_the_cli_is_accepted_by_the_loader(tmp_path, monkeypatch, source):
    """`python -m quant_engine.train` is the one path that vouches for per-symbol labels."""
    if source == "synthetic":
        args = ["--synthetic", "--n-bars", "2500"]
    else:
        monkeypatch.setattr(quant_train, "load_features_for_training", lambda *a, **k: _stub_postgres_frame())
        args = ["--symbols", "AAA,BBB"]
    monkeypatch.setattr(sys, "argv", ["train", *args, "--version", "cli", "--out", str(tmp_path)])

    quant_train.main()

    model = state.load_trend_model(tmp_path / "cli.joblib")
    assert model is not None
    assert model.artifact.label_scheme == LABEL_SCHEME_PER_SYMBOL_V2


def test_every_legacy_model_in_the_local_registry_is_refused_by_default():
    """ml/registry/trend is also train.py's default --out, so it can hold fresh per_symbol_v2
    models next to the gitignored v0/v1 ones; only the non-per_symbol_v2 ones must be refused."""
    registry = REPO_ROOT / "ml/registry/trend"
    legacy = [
        path
        for path in sorted(registry.glob("*.joblib"))
        if getattr(TrendModel.load(path).artifact, "label_scheme", None) != LABEL_SCHEME_PER_SYMBOL_V2
    ]
    if not legacy:
        pytest.skip("no legacy (non per_symbol_v2) models in ml/registry/trend (gitignored)")

    for path in legacy:
        assert state.load_trend_model(path) is None, f"{path.name} should be refused"


# --- broadcaster noise ------------------------------------------------------------


async def test_tick_skips_signal_push_when_legacy_stream_off(monkeypatch):
    monkeypatch.setattr(stream, "_LEGACY_SIGNAL_STREAM", False)
    push = AsyncMock()
    monkeypatch.setattr(stream, "_push_signals", push)
    regime = AsyncMock()
    monkeypatch.setattr(stream, "_push_regime", regime)

    await stream._tick(3)

    push.assert_not_called()
    regime.assert_awaited_once()


async def test_tick_pushes_signals_every_nth_tick_when_legacy_stream_on(monkeypatch):
    monkeypatch.setattr(stream, "_LEGACY_SIGNAL_STREAM", True)
    push = AsyncMock()
    monkeypatch.setattr(stream, "_push_signals", push)
    regime = AsyncMock()
    monkeypatch.setattr(stream, "_push_regime", regime)

    for tick in range(1, stream.SIGNAL_EVERY_N + 1):
        await stream._tick(tick)

    assert regime.await_count == stream.SIGNAL_EVERY_N  # regime on every tick
    push.assert_awaited_once()  # signals only on the Nth


async def test_tick_swallows_regime_failure_and_still_pushes_signals(monkeypatch):
    monkeypatch.setattr(stream, "_LEGACY_SIGNAL_STREAM", True)
    push = AsyncMock()
    monkeypatch.setattr(stream, "_push_signals", push)
    monkeypatch.setattr(stream, "_push_regime", AsyncMock(side_effect=RuntimeError("redis down")))

    await stream._tick(stream.SIGNAL_EVERY_N)

    push.assert_awaited_once()


async def test_tick_swallows_signal_failure(monkeypatch):
    monkeypatch.setattr(stream, "_LEGACY_SIGNAL_STREAM", True)
    monkeypatch.setattr(stream, "_push_regime", AsyncMock())
    monkeypatch.setattr(stream, "_push_signals", AsyncMock(side_effect=RuntimeError("boom")))

    await stream._tick(stream.SIGNAL_EVERY_N)  # must not raise


async def test_broadcaster_counts_ticks_only_while_clients_are_connected(monkeypatch):
    monkeypatch.setattr(stream, "BROADCAST_INTERVAL", 0.0)
    clients = {"connected": False}
    monkeypatch.setattr(stream.manager, "has_clients", lambda: clients["connected"])
    calls: list[int] = []
    third = asyncio.Event()

    async def fake_tick(tick: int) -> None:
        calls.append(tick)
        if len(calls) == 3:
            third.set()

    monkeypatch.setattr(stream, "_tick", fake_tick)

    task = asyncio.create_task(stream.broadcaster())
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert calls == []  # nobody connected: no work

        clients["connected"] = True
        await asyncio.wait_for(third.wait(), timeout=2)
        assert calls[:3] == [1, 2, 3]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("flag", [True, False])
async def test_broadcaster_logs_once_at_start_whether_legacy_stream_is_on(monkeypatch, flag):
    monkeypatch.setattr(stream, "_LEGACY_SIGNAL_STREAM", flag)
    spy = MagicMock()
    monkeypatch.setattr(stream, "log", spy)

    task = asyncio.create_task(stream.broadcaster())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    starts = [c for c in spy.info.call_args_list if c.args[0] == "stream.broadcaster.start"]
    assert len(starts) == 1
    assert starts[0].kwargs["legacy_signal_stream"] is flag


def _legacy_flag_in_fresh_interpreter(env_value: str | None) -> str:
    """Read the flag the way production does: once, at import, in a new process."""
    import os

    env = {k: v for k, v in os.environ.items() if k != "ATLAS_ENABLE_LEGACY_SIGNAL_STREAM"}
    if env_value is not None:
        env["ATLAS_ENABLE_LEGACY_SIGNAL_STREAM"] = env_value
    out = subprocess.run(
        [sys.executable, "-c", "import signal_service.stream as s; print(s._LEGACY_SIGNAL_STREAM)"],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip().splitlines()[-1]


def test_legacy_signal_stream_is_off_by_default():
    assert _legacy_flag_in_fresh_interpreter(None) == "False"
    assert _legacy_flag_in_fresh_interpreter("0") == "False"


def test_legacy_signal_stream_is_on_only_when_env_is_1():
    assert _legacy_flag_in_fresh_interpreter("1") == "True"
