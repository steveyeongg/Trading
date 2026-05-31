"""Per-process model registry. Phase 1 keeps it dumb: load on startup from
the on-disk artifact, refresh on SIGHUP."""

from __future__ import annotations

import os
from pathlib import Path

from atlas_shared.logging import get_logger
from quant_engine.trend import TrendModel

log = get_logger("signal-service.state")

_DEFAULT_PATH = Path(os.environ.get("ATLAS_TREND_MODEL", "ml/registry/trend/v0.joblib"))

_trend_model: TrendModel | None = None


def load_trend_model(path: Path | None = None) -> TrendModel | None:
    global _trend_model
    p = path or _DEFAULT_PATH
    if not p.exists():
        log.warning("trend_model.missing", path=str(p))
        _trend_model = None
        return None
    _trend_model = TrendModel.load(p)
    log.info("trend_model.loaded", path=str(p), version=_trend_model.version)
    return _trend_model


def get_trend_model() -> TrendModel | None:
    return _trend_model
