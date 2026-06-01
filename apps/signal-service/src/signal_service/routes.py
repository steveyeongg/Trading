"""HTTP routes for the signal service."""

from __future__ import annotations

from datetime import UTC, datetime

from atlas_shared.schemas import AssetClass, Horizon, Signal
from fastapi import APIRouter, HTTPException, Query
from ingest_equities.store import latest_bars
from macro_engine.refresh import load_snapshot
from news_ingest.retrieval import recent_sentiment_features
from pydantic import BaseModel, Field

from signal_service.pipeline import run_pipeline
from signal_service.providers import data_freshness_payload, provider_status_payload
from signal_service.screener import (
    ScreenerRow,
    ScreenerRun,
    list_universes,
    make_run_id,
    resolve_symbols,
)
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

    `resolution='1m'` reads raw 1-minute storage; any other supported value
    aggregates the underlying 1m bars on the fly via TimescaleDB
    `time_bucket()`. Currently supported: `1m`, `5m`, `15m`, `30m`, `1h`,
    `4h`, `1d`, `1w`.

    Returns 200 with an empty `bars` list when nothing is stored — the
    chart component renders an empty-state rather than erroring.
    """
    from ingest_equities.store import is_supported_resolution

    if not is_supported_resolution(resolution):
        raise HTTPException(400, f"unsupported resolution {resolution!r}")
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
        news_features=sentiment,
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
            news_features=sentiment,
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
    """Diagnostic view — exposes the sub-score breakdown, the active engine
    set, which gate (if any) killed the signal, and the veto reason. This is
    the single source of truth for answering "why is my signal null?".
    """
    import math

    from atlas_shared.schemas import Direction, SubScores
    from feature_engine import compute_features
    from macro_engine import s_macro
    from options_analytics import s_options
    from scoring_engine.composite import composite
    from scoring_engine.signal import (
        MIN_AGREE_THRESHOLD,
        MIN_COMPOSITE,
        MIN_CONFIDENCE,
        MIN_CONFIRMING_ENGINES,
        _build_active,
        _engines_agree,
    )
    from scoring_engine.sub_scores import s_liq, s_news, s_quant, s_risk, s_tech
    from sentiment_engine import s_sent

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
        news_features=sentiment,
        generate_explanation=False,
    )

    # ── Diagnostics: replay the early pipeline steps so the user can see
    # exactly which sub-scores fired and which gate failed. Cheap (microseconds)
    # and only on this debug-only endpoint.
    diagnostics: dict = {}
    if len(bars) >= 210:
        feats_df = compute_features(bars)
        latest = {
            k: v for k, v in feats_df.iloc[-1].to_dict().items()
            if not (isinstance(v, float) and math.isnan(v))
        }
        latest["close"] = float(bars["close"].iloc[-1])

        model = get_trend_model()
        p_up = model.predict_proba(feats_df) if model is not None else None
        macro_in = dict(snapshot or {}); macro_in.setdefault("regime", regime)
        subs = SubScores(
            tech=s_tech(latest),
            quant=s_quant(p_up),
            liq=s_liq(latest),
            risk=s_risk(latest),
            macro=s_macro(macro_in),
            sent=s_sent(sentiment or {}),
            news=s_news(sentiment or {}),
            opt=s_options({}),  # options never wired into prod route
        )
        active = _build_active(snapshot, sentiment, None, sentiment)
        score = composite(subs, active=active)
        long_count = _engines_agree(subs, Direction.LONG, MIN_AGREE_THRESHOLD, active)
        short_count = _engines_agree(subs, Direction.SHORT, MIN_AGREE_THRESHOLD, active)
        direction_implied = "long" if score >= 0 else "short"
        confirming = long_count if score >= 0 else short_count
        confidence = float(min(1.0, abs(p_up - 0.5) * 2.0)) if p_up is not None else min(1.0, abs(score) / 100.0)

        # Which gate failed (in pipeline order)?
        gate_failed: str | None = None
        if abs(score) < MIN_COMPOSITE:
            gate_failed = f"composite |{score:.1f}| < {MIN_COMPOSITE} (needs ≥{MIN_COMPOSITE})"
        elif confirming < MIN_CONFIRMING_ENGINES:
            gate_failed = (
                f"only {confirming} engine(s) confirm direction={direction_implied} "
                f"at ≥{MIN_AGREE_THRESHOLD} (need ≥{MIN_CONFIRMING_ENGINES})"
            )
        elif p_up is not None and confidence < MIN_CONFIDENCE:
            gate_failed = f"confidence {confidence:.2f} < {MIN_CONFIDENCE}"

        diagnostics = {
            "sub_scores": subs.model_dump(),
            "composite_score": round(score, 2),
            "direction_implied": direction_implied,
            "active_engines": sorted(active),
            "engines_confirming_long": long_count,
            "engines_confirming_short": short_count,
            "confidence": round(confidence, 3),
            "p_up": p_up,
            "trend_model_loaded": model is not None,
            "trend_model_version": getattr(model, "version", None),
            "gate_failed": gate_failed,
            "thresholds": {
                "MIN_COMPOSITE": MIN_COMPOSITE,
                "MIN_CONFIDENCE": MIN_CONFIDENCE,
                "MIN_CONFIRMING_ENGINES": MIN_CONFIRMING_ENGINES,
                "MIN_AGREE_THRESHOLD": MIN_AGREE_THRESHOLD,
            },
        }
    else:
        diagnostics = {
            "gate_failed": f"insufficient_bars: have {len(bars)}, need ≥210",
        }

    return {
        "signal": result.signal.model_dump(mode="json") if result.signal else None,
        "veto": {"reason": result.veto.reason, "detail": result.veto.detail} if result.veto else None,
        "no_signal_reason": result.no_signal_reason,
        "macro_snapshot": snapshot,
        "sentiment_snapshot": sentiment,
        "diagnostics": diagnostics,
    }


async def _safe_sentiment(symbol: str) -> dict[str, float]:
    """Pull recent sentiment; never fail the request if the news table is empty
    or Postgres can't be reached (we degrade to neutral)."""
    try:
        return await recent_sentiment_features(symbol)
    except Exception:
        return {"news_sent_score": 0.0, "news_sent_confidence": 0.0, "news_count": 0.0}


# ── Explanation — BLUEPRINT §10.3 ─────────────────────────────────────────────


@router.post("/explain/signal")
async def explain_signal(
    symbol: str = Query(..., description="Symbol to explain"),
    horizon: Horizon = Query(Horizon.SWING),
    resolution: str = Query("1m"),
    lookback: int = Query(500, ge=210, le=5000),
) -> dict:
    """Return the structured BLUEPRINT §10.3 explanation payload for the
    current signal (or the templated fallback if the gates failed). Cached
    locally by the writer using the §10.5 key — repeat calls are free."""
    from explanation_engine import generate_payload

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
        trend_model=get_trend_model(),
        regime=regime,
        macro_features=snapshot,
        sentiment_features=sentiment,
        news_features=sentiment,
        generate_explanation=False,
    )

    # We need a Signal object to feed the explanation writer; when the gates
    # killed the candidate, return the no-signal reason instead.
    if result.signal is None:
        return {
            "symbol": symbol,
            "explained": False,
            "no_signal_reason": result.no_signal_reason
                or (f"risk veto: {result.veto.reason}" if result.veto else "gated"),
        }

    # Re-derive the feature row for the explanation writer's context.
    from feature_engine import compute_features
    feats_df = compute_features(bars)
    latest_row = {
        k: v for k, v in feats_df.iloc[-1].to_dict().items()
        if not (isinstance(v, float) and v != v)  # filter NaN
    }
    payload = generate_payload(result.signal, latest_row)
    return {
        "symbol": symbol,
        "explained": True,
        "payload": payload.model_dump(),
        "signal": result.signal.model_dump(mode="json"),
    }


# ── Screener — BLUEPRINT §4 / §13.2 ───────────────────────────────────────────


class ScreenerRequest(BaseModel):
    """POST /v1/screener/run request body. BLUEPRINT §13.2."""

    universe: str = Field(
        default="manual",
        description='Universe key from /v1/screener/universes, or "manual" for symbol-only.',
    )
    symbols: list[str] = Field(
        default_factory=list,
        description="Manual symbol list. Required when universe == 'manual'.",
    )
    horizon: Horizon = Horizon.SWING
    asset_class: AssetClass = AssetClass.EQUITY
    resolution: str = "1m"
    min_composite: float = Field(default=60.0, ge=0.0, le=100.0)
    top_n: int | None = Field(default=10, ge=1, le=500)
    include_explanation: bool = False


# ── Provider health + data freshness — BLUEPRINT §13.1 ────────────────────────


@router.get("/providers/status")
async def providers_status() -> dict:
    """Configuration + availability of every external dependency.

    Missing keys never crash here — this endpoint exists specifically so the
    dashboard can show *which* features are degraded without inspecting logs.
    """
    return provider_status_payload()


@router.get("/data/freshness")
async def data_freshness(
    symbols: str = Query(
        "",
        description="Comma-separated list. Empty → falls back to the default watchlist.",
    ),
) -> dict:
    """Latest-bar age per symbol + macro snapshot age."""
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not syms:
        from signal_service.screener import load_universes
        syms = list(load_universes()["default_watchlist"]["symbols"])
    return await data_freshness_payload(syms)


@router.get("/screener/universes")
async def screener_universes() -> dict:
    """Built-in universes available to the screener. BLUEPRINT §4.3."""
    return {"universes": list_universes()}


@router.post("/screener/run")
async def screener_run(req: ScreenerRequest) -> dict:
    """Rank symbols by composite score with reasons attached.

    Manual mode: pass `universe="manual"` and a `symbols` list (BLUEPRINT §4.2).
    Universe mode: pass a key from `/v1/screener/universes`. Mix both to append
    one-off tickers to a named universe.

    Rows that don't publish a signal are still returned — with `no_signal_reason`
    populated — so the dashboard can show "why isn't NVDA on the list".
    """
    try:
        symbols = resolve_symbols(req.universe, req.symbols)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    snapshot = await load_snapshot()
    regime = (snapshot or {}).get("regime", "unknown")
    model = get_trend_model()

    rows: list[ScreenerRow] = []
    skipped: list[dict] = []

    for sym in symbols:
        bars = await latest_bars(sym, resolution=req.resolution, limit=500)
        if bars.empty:
            skipped.append({"symbol": sym, "reason": "no bars stored"})
            continue
        sentiment = await _safe_sentiment(sym)
        result = run_pipeline(
            bars=bars,
            symbol=sym,
            horizon=req.horizon,
            asset_class=req.asset_class,
            trend_model=model,
            regime=regime,
            macro_features=snapshot,
            sentiment_features=sentiment,
            news_features=sentiment,
            generate_explanation=req.include_explanation,
        )
        if result.signal is not None:
            rows.append(
                ScreenerRow(
                    symbol=sym,
                    signal=result.signal,
                    no_signal_reason=None,
                    composite_score=result.signal.composite_score,
                    direction_implied=result.signal.direction.value,
                )
            )
        else:
            # The veto path also surfaces a reason — prefer veto's detail when present.
            reason = result.no_signal_reason or (
                f"risk veto: {result.veto.reason}" if result.veto else "gated"
            )
            rows.append(
                ScreenerRow(
                    symbol=sym,
                    signal=None,
                    no_signal_reason=reason,
                    composite_score=None,
                    direction_implied=None,
                )
            )

    # Rank: published rows by |composite| desc; non-published rows after, in
    # input order so the user can still see the reasons they didn't publish.
    def _rank_key(r: ScreenerRow) -> tuple[int, float]:
        if r.signal is None:
            return (1, 0.0)
        return (0, -abs(r.signal.composite_score))

    rows.sort(key=_rank_key)

    published = [r for r in rows if r.signal is not None]
    if req.min_composite > 0:
        published = [
            r for r in published if abs(r.signal.composite_score) >= req.min_composite
        ]
    if req.top_n is not None:
        published = published[: req.top_n]

    # Non-published rows are appended after the filtered published set.
    final_rows = published + [r for r in rows if r.signal is None]

    run = ScreenerRun(
        run_id=make_run_id(),
        universe=req.universe,
        horizon=req.horizon,
        requested_symbols=symbols,
        min_composite=req.min_composite,
        top_n=req.top_n,
        generated_at=datetime.now(UTC),
        results=final_rows,
        skipped=skipped,
    )
    return run.to_payload(include_explanation=req.include_explanation)
