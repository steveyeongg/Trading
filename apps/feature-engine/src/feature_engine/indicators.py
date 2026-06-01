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
    # BLUEPRINT §5.2 — SMA institutional reference levels.
    "sma20",
    "sma50",
    "sma200",
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
    # BLUEPRINT §5.2 — additional trend / volatility / structure indicators.
    "supertrend",
    "supertrend_dir",
    "donchian_upper",
    "donchian_lower",
    "donchian_middle",
    "keltner_upper",
    "keltner_lower",
    "keltner_middle",
    "pivot",
    "pivot_r1",
    "pivot_s1",
    "fib_position",
    "fib_mid",
    "rvol_20",
    "high_52w_dist",
    "low_52w_dist",
    "gap_pct",
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


def _add_supertrend(df: pd.DataFrame, period: int, multiplier: float) -> None:
    """Supertrend line + direction (+1 trend up, -1 trend down). BLUEPRINT §5.2.

    Built on ATR with the standard upper/lower-band flip logic. Slightly more
    verbose than pandas-ta's helper but works on bar series of any length
    (pandas-ta returns NaN on short series in some versions)."""
    hl2 = (df["high"] + df["low"]) / 2.0
    atr = ta.atr(df["high"], df["low"], df["close"], length=period)
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    st = pd.Series(np.nan, index=df.index, dtype="float64")
    direction = pd.Series(0, index=df.index, dtype="int8")
    # Walk the series — vectorising the band-flip logic is awkward because each
    # step depends on the previous final band. O(n) but cheap.
    prev_st = np.nan
    prev_dir = 1
    for i in range(len(df)):
        if np.isnan(atr.iloc[i]):
            continue
        u, l = upper.iloc[i], lower.iloc[i]
        c = df["close"].iloc[i]
        if np.isnan(prev_st):
            prev_st = l
            prev_dir = 1
        if prev_dir == 1:
            cur = max(l, prev_st)
            if c < cur:
                prev_dir = -1
                cur = u
        else:
            cur = min(u, prev_st)
            if c > cur:
                prev_dir = 1
                cur = l
        st.iloc[i] = cur
        direction.iloc[i] = prev_dir
        prev_st = cur
    df["supertrend"] = st
    df["supertrend_dir"] = direction


def _add_donchian(df: pd.DataFrame, period: int) -> None:
    """Donchian channel — breakout detection. BLUEPRINT §5.2."""
    df["donchian_upper"] = df["high"].rolling(period, min_periods=1).max()
    df["donchian_lower"] = df["low"].rolling(period, min_periods=1).min()
    df["donchian_middle"] = (df["donchian_upper"] + df["donchian_lower"]) / 2.0


def _add_keltner(df: pd.DataFrame, period: int, multiplier: float) -> None:
    """Keltner channel — volatility-aware envelope around EMA. BLUEPRINT §5.2."""
    ema_mid = ta.ema(df["close"], length=period)
    atr = ta.atr(df["high"], df["low"], df["close"], length=period)
    df["keltner_middle"] = ema_mid
    df["keltner_upper"] = ema_mid + multiplier * atr
    df["keltner_lower"] = ema_mid - multiplier * atr


def _add_pivots(df: pd.DataFrame) -> None:
    """Classic floor pivots from the *previous* bar's H/L/C. BLUEPRINT §5.2.

    These are intentionally bar-rolling rather than session-rolling — for
    intraday timeframes that produces too much churn to be useful, but the
    composite scorer just uses pivot_r1 / pivot_s1 distance as another
    support/resistance reference, not a hard level."""
    pc = df["close"].shift(1)
    ph = df["high"].shift(1)
    pl = df["low"].shift(1)
    df["pivot"] = (ph + pl + pc) / 3.0
    df["pivot_r1"] = 2 * df["pivot"] - pl
    df["pivot_s1"] = 2 * df["pivot"] - ph


def _add_fibonacci(df: pd.DataFrame, lookback: int) -> None:
    """Fibonacci position within the recent `lookback`-bar swing range.

    `fib_position` ∈ [0, 1] is the close's position between the rolling low
    and rolling high — 0 at swing low, 1 at swing high. `fib_mid` is the
    50% retracement price level."""
    hi = df["high"].rolling(lookback, min_periods=1).max()
    lo = df["low"].rolling(lookback, min_periods=1).min()
    span = (hi - lo).replace(0, np.nan)
    df["fib_position"] = ((df["close"] - lo) / span).clip(0.0, 1.0)
    df["fib_mid"] = (hi + lo) / 2.0


def _add_relative_volume(df: pd.DataFrame, window: int) -> None:
    """Volume / 20-bar average volume. >1 = above-normal participation."""
    avg = df["volume"].rolling(window, min_periods=1).mean().replace(0, np.nan)
    df["rvol_20"] = df["volume"] / avg


def _add_52w_distance(df: pd.DataFrame, window: int) -> None:
    """% distance from the rolling 52-week high / low.

    `high_52w_dist` < 0 (close is below the high). `low_52w_dist` > 0 (close is
    above the low). Uses `min_periods=1` so it produces values from bar 0 —
    callers should be aware that early values are unreliable on short series."""
    hi = df["high"].rolling(window, min_periods=1).max()
    lo = df["low"].rolling(window, min_periods=1).min()
    df["high_52w_dist"] = (df["close"] - hi) / hi.replace(0, np.nan)
    df["low_52w_dist"] = (df["close"] - lo) / lo.replace(0, np.nan)


def _add_gap_pct(df: pd.DataFrame) -> None:
    """Open vs previous close, expressed as a percent. BLUEPRINT §5.2."""
    prev_close = df["close"].shift(1)
    df["gap_pct"] = (df["open"] - prev_close) / prev_close.replace(0, np.nan)


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
    # SMA institutional reference levels — same lookback as EMA200 max.
    df["sma20"] = df["close"].rolling(20, min_periods=1).mean()
    df["sma50"] = df["close"].rolling(50, min_periods=1).mean()
    df["sma200"] = df["close"].rolling(200, min_periods=1).mean()
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

    # --- BLUEPRINT §5.2 additional indicators ---
    _add_supertrend(df, period=10, multiplier=3.0)
    _add_donchian(df, period=20)
    _add_keltner(df, period=20, multiplier=2.0)
    _add_pivots(df)
    _add_fibonacci(df, lookback=50)
    _add_relative_volume(df, window=20)
    _add_52w_distance(df, window=252)
    _add_gap_pct(df)

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
