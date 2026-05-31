"""Regime classifier — KMeans over rolling macro features.

The BLUEPRINT mentions HMM and KMeans; HMM gives smoother regime probabilities
but adds a numpy-only HMM dependency. Phase 2 starts with KMeans; we can swap
in `hmmlearn` later without changing the public interface.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

REGIMES = ("risk-off", "late-cycle", "risk-on", "stagflation")


@dataclass
class RegimeOutput:
    label: str
    probabilities: dict[str, float]
    confidence: float


def _features(series: dict[str, pd.Series]) -> pd.DataFrame:
    """Build the macro feature matrix from raw FRED series.

    Columns chosen for *regime separability*, not forecast — these are the
    inputs the literature cites for risk-on / risk-off classification.
    """
    if not series:
        return pd.DataFrame()

    df = pd.concat(
        {k: v for k, v in series.items() if not v.empty},
        axis=1,
        join="inner",
    ).ffill().dropna()
    if df.empty:
        return df

    out = pd.DataFrame(index=df.index)
    if "T10Y2Y" in df:
        out["term_spread"] = df["T10Y2Y"]
    if "VIXCLS" in df:
        out["vix"] = df["VIXCLS"]
        out["vix_z"] = _rolling_z(df["VIXCLS"], 60)
    if "DTWEXBGS" in df:
        out["dxy_z"] = _rolling_z(df["DTWEXBGS"], 60)
    if "DGS10" in df and "DGS2" in df:
        out["curve_slope"] = df["DGS10"] - df["DGS2"]
    if "CPIAUCSL" in df:
        out["cpi_yoy"] = df["CPIAUCSL"].pct_change(252)
    return out.dropna()


def _rolling_z(s: pd.Series, window: int) -> pd.Series:
    mean = s.rolling(window).mean()
    std = s.rolling(window).std(ddof=0)
    return (s - mean) / std.replace(0, np.nan)


class RegimeClassifier:
    """KMeans clusterer with deterministic regime labels.

    The K=4 clusters are mapped to the canonical regime names by the *centroid*
    on (vix_z, term_spread, cpi_yoy) — high vix + flat curve = risk-off, etc.
    Falls back to "unknown" when the feature frame lacks the columns needed
    to disambiguate.
    """

    def __init__(self, n_clusters: int = 4, random_state: int = 7):
        self.n_clusters = n_clusters
        self.random_state = random_state
        self._scaler: StandardScaler | None = None
        self._km: KMeans | None = None
        self._labels: list[str] | None = None

    def fit(self, series: dict[str, pd.Series]) -> RegimeClassifier:
        feats = _features(series)
        if len(feats) < 50:
            return self
        self._scaler = StandardScaler().fit(feats.to_numpy())
        X = self._scaler.transform(feats.to_numpy())
        self._km = KMeans(n_clusters=self.n_clusters, n_init=10, random_state=self.random_state)
        self._km.fit(X)
        self._labels = self._assign_regime_names(feats, self._km.labels_)
        return self

    def _assign_regime_names(self, feats: pd.DataFrame, cluster_labels: np.ndarray) -> list[str]:
        """Map cluster id → regime name by inspecting cluster centroids."""
        labels: list[str | None] = [None] * self.n_clusters
        df = feats.copy()
        df["cluster"] = cluster_labels
        centroids = df.groupby("cluster").mean()

        used: set[str] = set()
        # Risk-off: highest vix_z.
        if "vix_z" in centroids:
            c = centroids["vix_z"].idxmax()
            labels[c] = "risk-off"
            used.add("risk-off")
        # Risk-on: lowest vix_z that isn't risk-off.
        if "vix_z" in centroids:
            candidates = centroids[centroids.index != centroids["vix_z"].idxmax()]
            c = candidates["vix_z"].idxmin()
            if labels[c] is None:
                labels[c] = "risk-on"
                used.add("risk-on")
        # Stagflation: highest cpi_yoy among remaining.
        if "cpi_yoy" in centroids:
            remaining = centroids[[labels[i] is None for i in centroids.index]]
            if not remaining.empty and "stagflation" not in used:
                c = remaining["cpi_yoy"].idxmax()
                labels[c] = "stagflation"
                used.add("stagflation")
        # Late-cycle gets whatever's left.
        for i in range(self.n_clusters):
            if labels[i] is None:
                labels[i] = "late-cycle"
        return [str(x) for x in labels]

    def classify(self, series: dict[str, pd.Series]) -> RegimeOutput:
        """Return the current regime label + soft probabilities."""
        if self._km is None or self._scaler is None or self._labels is None:
            return RegimeOutput(label="unknown", probabilities={r: 0.25 for r in REGIMES}, confidence=0.0)
        feats = _features(series)
        if feats.empty:
            return RegimeOutput(label="unknown", probabilities={r: 0.25 for r in REGIMES}, confidence=0.0)

        raw = feats.iloc[[-1]].to_numpy()
        if not np.all(np.isfinite(raw)):
            return RegimeOutput(label="unknown", probabilities={r: 0.25 for r in REGIMES}, confidence=0.0)
        x = self._scaler.transform(raw)
        # Belt-and-suspenders: a degenerate fit-time std can produce NaN/Inf.
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        distances = self._km.transform(x).ravel()
        distances = np.nan_to_num(distances, nan=np.inf, posinf=np.inf)
        if not np.any(np.isfinite(distances)):
            return RegimeOutput(label="unknown", probabilities={r: 0.25 for r in REGIMES}, confidence=0.0)
        # Soft assignment via inverse distance.
        inv = 1.0 / (distances + 1e-9)
        probs_per_cluster = inv / inv.sum()

        probs: dict[str, float] = {r: 0.0 for r in REGIMES}
        for cluster_id, p in enumerate(probs_per_cluster):
            name = self._labels[cluster_id]
            probs[name] = probs.get(name, 0.0) + float(p)

        label = max(probs.items(), key=lambda kv: kv[1])[0]
        return RegimeOutput(label=label, probabilities=probs, confidence=probs[label])
