"""Per-process model registry. Phase 1 keeps it dumb: load on startup from
the on-disk artifact, refresh on SIGHUP."""

from __future__ import annotations

import os
from pathlib import Path

from atlas_shared.logging import get_logger
from quant_engine.trend import LABEL_SCHEME_PER_SYMBOL_V2, TrendModel

log = get_logger("signal-service.state")

_DEFAULT_PATH = Path(os.environ.get("ATLAS_TREND_MODEL", "ml/registry/trend/v0.joblib"))

_trend_model: TrendModel | None = None


def load_trend_model(path: Path | None = None) -> TrendModel | None:
    """Load the trend model, refusing artifacts trained on leaked labels.

    Models trained before per-symbol labelling (v0, v1) learned each ticker's price
    level from labels computed on the (ts, symbol)-interleaved series; their reported
    AUC does not reflect any skill. They are refused unless ATLAS_ALLOW_LEGACY_MODEL=1
    (read per call, not at import). A refusal leaves no model loaded.
    """
    global _trend_model
    p = path or _DEFAULT_PATH
    if not p.exists():
        log.warning("trend_model.missing", path=str(p))
        _trend_model = None
        return None
    model = TrendModel.load(p)
    # getattr: a pickle from before the field existed has no such attribute.
    scheme = getattr(model.artifact, "label_scheme", None)
    if scheme != LABEL_SCHEME_PER_SYMBOL_V2:
        if os.environ.get("ATLAS_ALLOW_LEGACY_MODEL") != "1":
            log.warning(
                "trend_model.legacy_refused",
                path=str(p),
                version=model.version,
                label_scheme=scheme,
                hint="trained on leaked labels; retrain, or set ATLAS_ALLOW_LEGACY_MODEL=1",
            )
            _trend_model = None
            return None
        log.warning(
            "trend_model.legacy_allowed",
            path=str(p),
            version=model.version,
            label_scheme=scheme,
        )
    _trend_model = model
    log.info("trend_model.loaded", path=str(p), version=_trend_model.version)
    return _trend_model


def get_trend_model() -> TrendModel | None:
    return _trend_model
