"""Triple-barrier labeling (López de Prado, AFML ch. 3).

For each event at time t, three barriers race:
  - Upper barrier:  entry * (1 + pt * volatility[t])  — profit-take
  - Lower barrier:  entry * (1 - sl * volatility[t])  — stop-loss
  - Vertical:       t + horizon                       — time stop
The label is +1 if upper is hit first, -1 if lower is hit first, 0 otherwise.

We use ATR-normalised barriers so the labels are comparable across regimes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def triple_barrier_labels(
    close: pd.Series,
    atr: pd.Series,
    horizon_bars: int = 20,
    pt_mult: float = 2.0,
    sl_mult: float = 1.0,
) -> pd.Series:
    """Return a Series of labels in {-1, 0, +1} aligned to `close.index`.

    Bars near the end of the series get NaN because the horizon would run off
    the data.
    """
    if len(close) != len(atr):
        raise ValueError("close and atr must align")

    c = close.to_numpy()
    a = atr.to_numpy()
    n = len(c)
    out = np.full(n, np.nan)

    for i in range(n - horizon_bars):
        if not np.isfinite(a[i]) or a[i] <= 0:
            continue
        up = c[i] + pt_mult * a[i]
        dn = c[i] - sl_mult * a[i]
        window = c[i + 1 : i + 1 + horizon_bars]
        hit_up = np.where(window >= up)[0]
        hit_dn = np.where(window <= dn)[0]
        first_up = hit_up[0] if hit_up.size else np.inf
        first_dn = hit_dn[0] if hit_dn.size else np.inf
        if first_up < first_dn:
            out[i] = 1.0
        elif first_dn < first_up:
            out[i] = -1.0
        else:
            out[i] = 0.0

    return pd.Series(out, index=close.index, name="label")


def binary_up_labels(labels: pd.Series) -> pd.Series:
    """Collapse triple-barrier {-1, 0, +1} into {0, 1} for binary classification.

    +1 → 1, everything else → 0. The model thus learns "will the upper barrier
    fire before the lower one within the horizon".
    """
    return (labels == 1).astype("int8")
