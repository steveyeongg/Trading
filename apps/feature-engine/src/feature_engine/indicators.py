"""Indicator catalogue + computation. Phase 1 implementation.

Contract: given a pandas DataFrame with columns
    ["ts", "open", "high", "low", "close", "volume"]
sorted by ts ascending, return a new DataFrame of the same length with the
indicator columns listed in `INDICATORS` plus the originals. NaNs are allowed
at the head where lookback is insufficient — downstream code must handle them
explicitly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta as ta

INDICATORS: list[str] = [
    "rsi14",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_pctb",
    "ema9",
    "ema21",
    "ema50",
    "ema200",
    "atr14",
    "adx",
    "di_plus",
    "di_minus",
    "vwap_session",
    "obv",
    "obv_slope_z",
    "stoch_k",
    "stoch_d",
    "ichimoku_tenkan",
    "ichimoku_kijun",
    "ichimoku_span_a",
    "ichimoku_span_b",
    "divergence_bull",
    "divergence_bear",
    "smc_bos",
    "ret_log",
    "vol_realized_20",
]


def _bos(close: pd.Series, lookback: int = 20) -> pd.Series:
    """Coarse break-of-structure flag.

    +1 when close crosses *above* the rolling high of the previous `lookback`
    bars; -1 when it crosses *below* the rolling low. 0 otherwise. This is a
    crude proxy for SMC BOS that's good enough for a sub-score input; the
    full SMC engine is Phase 2.
    """
    prev_high = close.shift(1).rolling(lookback).max()
    prev_low = close.shift(1).rolling(lookback).min()
    bos = pd.Series(0, index=close.index, dtype="int8")
    bos[close > prev_high] = 1
    bos[close < prev_low] = -1
    return bos


def _divergences(close: pd.Series, rsi: pd.Series, lookback: int = 14) -> tuple[pd.Series, pd.Series]:
    """Classic RSI divergence flags over a rolling window.

    Bullish: price prints lower low AND rsi prints higher low.
    Bearish: price prints higher high AND rsi prints lower high.
    Both are weak signals in isolation — the composite scorer treats them as
    one of several inputs, not a standalone trigger.
    """
    price_min = close.rolling(lookback).min()
    price_max = close.rolling(lookback).max()
    rsi_min = rsi.rolling(lookback).min()
    rsi_max = rsi.rolling(lookback).max()

    prev_price_min = price_min.shift(lookback)
    prev_price_max = price_max.shift(lookback)
    prev_rsi_min = rsi_min.shift(lookback)
    prev_rsi_max = rsi_max.shift(lookback)

    bull = ((price_min < prev_price_min) & (rsi_min > prev_rsi_min)).astype("int8")
    bear = ((price_max > prev_price_max) & (rsi_max < prev_rsi_max)).astype("int8")
    return bull, bear


def _zscore(s: pd.Series, window: int = 20) -> pd.Series:
    mean = s.rolling(window).mean()
    std = s.rolling(window).std(ddof=0)
    return (s - mean) / std.replace(0, np.nan)


def compute_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Compute the full Phase 1 feature set."""
    if bars.empty:
        return bars.copy()

    required = {"ts", "open", "high", "low", "close", "volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    df = bars.copy().reset_index(drop=True)

    # --- Momentum ---
    df["rsi14"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df["macd_hist"] = macd["MACDh_12_26_9"]

    # --- Volatility / bands ---
    bb = ta.bbands(df["close"], length=20, std=2.0)
    # Newer pandas-ta (>=0.4) double-stamps the std in the column suffix.
    # Resolve by prefix to tolerate either naming.
    bb_cols = {c.split("_")[0]: c for c in bb.columns}
    df["bb_upper"] = bb[bb_cols["BBU"]]
    df["bb_middle"] = bb[bb_cols["BBM"]]
    df["bb_lower"] = bb[bb_cols["BBL"]]
    df["bb_pctb"] = bb[bb_cols["BBP"]]
    df["atr14"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # --- Trend ---
    df["ema9"] = ta.ema(df["close"], length=9)
    df["ema21"] = ta.ema(df["close"], length=21)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)
    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    df["adx"] = adx["ADX_14"]
    df["di_plus"] = adx["DMP_14"]
    df["di_minus"] = adx["DMN_14"]

    # --- Volume ---
    # pandas-ta's VWAP requires a DatetimeIndex; we compute a session-cumulative
    # VWAP manually so it works on any bar series.
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vwap_session"] = (tp * df["volume"]).cumsum() / df["volume"].replace(0, np.nan).cumsum()
    df["obv"] = ta.obv(df["close"], df["volume"])
    obv_slope = df["obv"].diff()
    df["obv_slope_z"] = _zscore(obv_slope, window=20)

    # --- Oscillators ---
    stoch = ta.stoch(df["high"], df["low"], df["close"], k=14, d=3, smooth_k=3)
    df["stoch_k"] = stoch["STOCHk_14_3_3"]
    df["stoch_d"] = stoch["STOCHd_14_3_3"]

    # --- Ichimoku ---
    # pandas-ta returns two frames (visible spans + a forward-shifted span).
    # We only consume the visible spans here.
    ich, _ = ta.ichimoku(df["high"], df["low"], df["close"])
    df["ichimoku_tenkan"] = ich["ITS_9"]
    df["ichimoku_kijun"] = ich["IKS_26"]
    df["ichimoku_span_a"] = ich["ISA_9"]
    df["ichimoku_span_b"] = ich["ISB_26"]

    # --- Custom / structure ---
    bull, bear = _divergences(df["close"], df["rsi14"], lookback=14)
    df["divergence_bull"] = bull
    df["divergence_bear"] = bear
    df["smc_bos"] = _bos(df["close"], lookback=20)

    # --- Returns / realized vol ---
    df["ret_log"] = np.log(df["close"] / df["close"].shift(1))
    df["vol_realized_20"] = df["ret_log"].rolling(20).std(ddof=0) * np.sqrt(252)

    # MTF confirmation flag is computed by the caller (it needs multiple
    # resolutions); leave it None here. The scoring engine treats absence
    # conservatively (no boost).
    return df


def latest_feature_row(bars: pd.DataFrame) -> dict[str, float]:
    """Compute features and return only the last row as a dict.

    Convenient for the live signal path where we only need the most recent
    feature vector and not the full history.
    """
    df = compute_features(bars)
    if df.empty:
        return {}
    row = df.iloc[-1].to_dict()
    return {k: (None if pd.isna(v) else v) for k, v in row.items()}
