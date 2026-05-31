"""HTTP routes for the signal service."""

from __future__ import annotations

from atlas_shared.schemas import AssetClass, Horizon, Signal
from fastapi import APIRouter, HTTPException, Query
from ingest_equities.store import latest_bars
from macro_engine.refresh import load_snapshot
from news_ingest.retrieval import recent_sentiment_features

from signal_service.pipeline import run_pipeline
from signal_service.state import get_trend_model

router = APIRouter(prefix="/v1")


@router.get("/regime")
async def regime() -> dict:
    """Current macro regime snapshot. Cached in Redis by `macro_engine.refresh`."""
    snapshot = await load_snapshot()
    if snapshot is None:
        return {"regime": "unknown", "regime_confidence": 0.0, "regime_probabilities": {}}
    return snapshot


@router.get("/symbols/{symbol}/bars")
async def symbol_bars(
    symbol: str,
    resolution: str = Query("1m"),
    limit: int = Query(300, ge=1, le=5000),
) -> dict:
    """Recent OHLCV bars for charting. Shaped for lightweight-charts:
    each bar carries an epoch-second `time` plus o/h/l/c and volume.

    Returns 200 with an empty `bars` list when nothing is stored — the
    chart component renders an empty-state rather than erroring.
    """
    symbol = symbol.upper()
    bars = await latest_bars(symbol, resolution=resolution, limit=limit)
    if bars.empty:
        return {"symbol": symbol, "resolution": resolution, "bars": []}

    out = [
        {
            "time": int(row["ts"].timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for _, row in bars.iterrows()
    ]
    return {"symbol": symbol, "resolution": resolution, "bars": out}


@router.get("/symbols/{symbol}/options")
async def symbol_options(symbol: str) -> dict:
    """Options analytics (put/call, IV rank, max-pain, GEX) + the s_opt
    sub-score for the symbol.

    With no options-data vendor configured this synthesizes a plausible chain
    off the latest bar close so the analytics are demonstrable. `synthetic:
    true` makes that explicit — do not trade off it. A real feed adapter drops
    in behind the same shape.
    """
    from options_analytics import compute_features, s_options, synthetic_chain

    symbol = symbol.upper()
    # The chain is synthetic regardless, so a missing DB shouldn't 500 — fall
    # back to a nominal spot so the analytics are always demonstrable.
    try:
        bars = await latest_bars(symbol, resolution="1m", limit=1)
        spot = float(bars["close"].iloc[-1]) if not bars.empty else 100.0
    except Exception:
        spot = 100.0
    chain = synthetic_chain(symbol, spot)
    feats = compute_features(chain)
    fd = {
        "put_call_oi": feats.put_call_oi,
        "iv_rank": feats.iv_rank,
        "gex": feats.gex,
        "max_pain_distance": feats.max_pain_distance,
    }
    return {
        "symbol": symbol,
        "spot": spot,
        "synthetic": True,
        "features": {
            "put_call_oi": feats.put_call_oi,
            "put_call_volume": feats.put_call_volume,
            "iv30": feats.iv30,
            "iv_rank": feats.iv_rank,
            "max_pain": feats.max_pain,
            "max_pain_distance": feats.max_pain_distance,
            "gex": feats.gex,
            "gex_flip": feats.gex_flip,
        },
        "s_opt": s_options(fd),
    }


@router.get("/signals/{symbol}", response_model=Signal | None)
async def signal_for_symbol(
    symbol: str,
    horizon: Horizon = Query(Horizon.SWING),
    asset_class: AssetClass = Query(AssetClass.EQUITY),
    resolution: str = Query("1m"),
    lookback: int = Query(500, ge=210, le=5000),
    explain: bool = Query(True, description="Run the LLM rationale writer."),
) -> Signal | None:
    """Compute the current signal for `symbol` from the latest stored bars.

    Returns 200 with `null` if no signal passes the scoring or risk gates —
    that's normal, not an error.
    """
    symbol = symbol.upper()
    bars = await latest_bars(symbol, resolution=resolution, limit=lookback)
    if bars.empty:
        raise HTTPException(404, f"no bars stored for {symbol} @ {resolution}")

    snapshot = await load_snapshot()
    regime = (snapshot or {}).get("regime", "unknown")
    sentiment = await _safe_sentiment(symbol)

    result = run_pipeline(
        bars=bars,
        symbol=symbol,
        horizon=horizon,
        asset_class=asset_class,
        trend_model=get_trend_model(),
        regime=regime,
        macro_features=snapshot,
        sentiment_features=sentiment,
        generate_explanation=explain,
    )
    return result.signal


@router.post("/scan", response_model=list[Signal])
async def scan(
    symbols: list[str],
    horizon: Horizon = Horizon.SWING,
    min_score: float = 50.0,
    resolution: str = "1m",
    explain: bool = False,
) -> list[Signal]:
    """Ad-hoc multi-symbol scan. Filters to `|composite| >= min_score`.

    LLM explanations are off by default for scans — generating 100 rationales
    is expensive and rarely consumed.
    """
    out: list[Signal] = []
    model = get_trend_model()
    snapshot = await load_snapshot()
    regime = (snapshot or {}).get("regime", "unknown")
    for sym in (s.upper() for s in symbols):
        bars = await latest_bars(sym, resolution=resolution, limit=500)
        if bars.empty:
            continue
        sentiment = await _safe_sentiment(sym)
        result = run_pipeline(
            bars=bars,
            symbol=sym,
            horizon=horizon,
            trend_model=model,
            regime=regime,
            macro_features=snapshot,
            sentiment_features=sentiment,
            generate_explanation=explain,
        )
        if result.signal and abs(result.signal.composite_score) >= min_score:
            out.append(result.signal)
    return out


@router.get("/signals/{symbol}/debug")
async def signal_debug(
    symbol: str,
    horizon: Horizon = Query(Horizon.SWING),
    asset_class: AssetClass = Query(AssetClass.EQUITY),
    resolution: str = Query("1m"),
    lookback: int = Query(500, ge=210, le=5000),
) -> dict:
    """Diagnostic view — exposes the veto reason when a signal is killed."""
    symbol = symbol.upper()
    bars = await latest_bars(symbol, resolution=resolution, limit=lookback)
    if bars.empty:
        raise HTTPException(404, f"no bars stored for {symbol} @ {resolution}")

    snapshot = await load_snapshot()
    regime = (snapshot or {}).get("regime", "unknown")
    sentiment = await _safe_sentiment(symbol)

    result = run_pipeline(
        bars=bars,
        symbol=symbol,
        horizon=horizon,
        asset_class=asset_class,
        trend_model=get_trend_model(),
        regime=regime,
        macro_features=snapshot,
        sentiment_features=sentiment,
        generate_explanation=False,
    )
    return {
        "signal": result.signal.model_dump(mode="json") if result.signal else None,
        "veto": {"reason": result.veto.reason, "detail": result.veto.detail} if result.veto else None,
        "macro_snapshot": snapshot,
        "sentiment_snapshot": sentiment,
    }


async def _safe_sentiment(symbol: str) -> dict[str, float]:
    """Pull recent sentiment; never fail the request if the news table is empty
    or Postgres can't be reached (we degrade to neutral)."""
    try:
        return await recent_sentiment_features(symbol)
    except Exception:
        return {"news_sent_score": 0.0, "news_sent_confidence": 0.0, "news_count": 0.0}
