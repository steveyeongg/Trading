"""WebSocket live push.

Clients connect to `/v1/stream` and send a subscribe frame:
    {"subscribe": ["regime.global", "signals.AAPL", "signals.*"]}

The server runs ONE broadcaster loop (not per-client polling) that:
  - every BROADCAST_INTERVAL pushes `regime.global` (cheap — reads Redis),
  - every SIGNAL_EVERY_N ticks recomputes signals for symbols that have at
    least one subscriber and pushes `signals.{symbol}`.

Subjects support a trailing `*` wildcard (`signals.*` matches `signals.AAPL`).
This is "server-recomputes, client-receives" — it removes client polling and
gives a live feel without a full event bus (that's the Kafka path, Phase 2+).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from alert_service import AlertEngine, list_all_rules, record_delivery
from atlas_shared import metrics as mx
from atlas_shared.logging import get_logger
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ingest_equities.store import latest_bars
from macro_engine.refresh import load_snapshot

from signal_service.pipeline import run_pipeline
from signal_service.state import get_trend_model

log = get_logger("stream")
router = APIRouter(prefix="/v1")

BROADCAST_INTERVAL = 5.0     # seconds between regime pushes
SIGNAL_EVERY_N = 3           # recompute signals every Nth tick (→ 15s)

# One alert engine per process — holds the cooldown map across ticks.
_alert_engine = AlertEngine()


class ConnectionManager:
    def __init__(self) -> None:
        self._subs: dict[WebSocket, set[str]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._subs[ws] = set()
        mx.WS_CONNECTIONS.inc()

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            existed = self._subs.pop(ws, None) is not None
        if existed:
            mx.WS_CONNECTIONS.dec()

    async def subscribe(self, ws: WebSocket, subjects: list[str]) -> None:
        async with self._lock:
            if ws in self._subs:
                self._subs[ws].update(subjects)

    async def unsubscribe(self, ws: WebSocket, subjects: list[str]) -> None:
        async with self._lock:
            if ws in self._subs:
                self._subs[ws].difference_update(subjects)

    def _matches(self, subscribed: set[str], subject: str) -> bool:
        if subject in subscribed:
            return True
        return any(s.endswith("*") and subject.startswith(s[:-1]) for s in subscribed)

    async def publish(self, subject: str, data: Any) -> None:
        async with self._lock:
            targets = [ws for ws, subs in self._subs.items() if self._matches(subs, subject)]
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json({"subject": subject, "data": data})
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    def subscribed_symbols(self) -> set[str]:
        """Symbols any client wants signal updates for (excludes wildcards)."""
        out: set[str] = set()
        for subs in self._subs.values():
            for s in subs:
                if s.startswith("signals.") and not s.endswith("*"):
                    out.add(s.split(".", 1)[1].upper())
        return out

    def has_clients(self) -> bool:
        return bool(self._subs)


manager = ConnectionManager()


async def _regime_payload() -> dict:
    snapshot = await load_snapshot()
    return snapshot or {"regime": "unknown", "regime_confidence": 0.0, "regime_probabilities": {}}


async def _push_regime() -> None:
    await manager.publish("regime.global", await _regime_payload())


async def _push_signals() -> None:
    symbols = manager.subscribed_symbols()
    if not symbols:
        return
    snapshot = await load_snapshot()
    regime = (snapshot or {}).get("regime", "unknown")
    model = get_trend_model()

    # Load all users' enabled rules once per push cycle (cheap; engine holds
    # cooldown keyed by rule_id, which is globally unique).
    rules = []
    with contextlib.suppress(Exception):
        rules = await list_all_rules()

    for sym in symbols:
        try:
            bars = await latest_bars(sym, resolution="1m", limit=500)
            if bars.empty:
                continue
            # run_pipeline is sync + CPU-bound — offload so the loop stays free.
            result = await asyncio.to_thread(
                run_pipeline,
                bars=bars,
                symbol=sym,
                trend_model=model,
                regime=regime,
                macro_features=snapshot,
                generate_explanation=False,
            )
            payload = {
                "signal": result.signal.model_dump(mode="json") if result.signal else None,
                "veto": {"reason": result.veto.reason, "detail": result.veto.detail} if result.veto else None,
                "no_signal_reason": result.no_signal_reason,
            }
            await manager.publish(f"signals.{sym}", payload)

            # Fire alerts on the freshly computed signal (cooldown-gated).
            # Pass the latest close + regime so §12.2 event derivation can
            # detect price-level breaches and regime changes.
            if result.signal and rules:
                with contextlib.suppress(Exception):
                    last_close = float(bars["close"].iloc[-1])
                    await _alert_engine.evaluate_signal(
                        payload["signal"],
                        rules,
                        last_price=last_close,
                        regime=regime,
                        record=record_delivery,
                    )
        except Exception as e:
            log.warning("stream.signal_push_failed", symbol=sym, error=str(e))


async def broadcaster() -> None:
    """Single background loop. Started in the app lifespan."""
    log.info("stream.broadcaster.start", interval=BROADCAST_INTERVAL)
    tick = 0
    try:
        while True:
            await asyncio.sleep(BROADCAST_INTERVAL)
            if not manager.has_clients():
                continue
            tick += 1
            with contextlib.suppress(Exception):
                await _push_regime()
            if tick % SIGNAL_EVERY_N == 0:
                with contextlib.suppress(Exception):
                    await _push_signals()
    except asyncio.CancelledError:
        log.info("stream.broadcaster.stop")
        raise


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        # Push an immediate regime snapshot *directly to this socket* so the
        # client paints without waiting for the first broadcast tick. (Going
        # via publish() would match nothing — the client hasn't subscribed.)
        await ws.send_json({"subject": "regime.global", "data": await _regime_payload()})
        while True:
            msg = await ws.receive_json()
            if "subscribe" in msg:
                await manager.subscribe(ws, list(msg["subscribe"]))
                await ws.send_json({"subject": "_ack", "data": {"subscribed": msg["subscribe"]}})
            elif "unsubscribe" in msg:
                await manager.unsubscribe(ws, list(msg["unsubscribe"]))
            elif "ping" in msg:
                await ws.send_json({"subject": "_pong", "data": {}})
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception as e:
        log.warning("stream.error", error=str(e))
        await manager.disconnect(ws)
