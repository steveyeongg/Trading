"""Alert engine: evaluate a signal against rules, respect cooldown, dispatch.

The engine holds the channel registry and an in-memory cooldown map. It is
driven by the stream broadcaster (which already recomputes signals on a
cadence) — one call to `evaluate_signal` per (symbol, signal) tick.

Cooldown state is in-memory: fine for a single process. Multi-replica
deployments move it to Redis (Phase 3) — the interface stays identical.
"""

from __future__ import annotations

import time
from typing import Any

from atlas_shared import metrics as mx
from atlas_shared.logging import get_logger

from alert_service.channels import build_channels
from alert_service.rules import AlertRule, matches, metric_value

log = get_logger("alert.engine")


class AlertEngine:
    def __init__(self) -> None:
        self.channels = build_channels()
        # (rule_id, symbol) -> last fired epoch seconds.
        self._last_fired: dict[tuple[str, str], float] = {}

    def _cooled_down(self, rule: AlertRule, symbol: str, now: float) -> bool:
        last = self._last_fired.get((rule.id, symbol))
        return last is None or (now - last) >= rule.cooldown_s

    async def evaluate_signal(
        self,
        signal: dict[str, Any],
        rules: list[AlertRule],
        record=None,  # optional async (rule_id, symbol, channel, ok, detail, payload) -> None
    ) -> list[dict]:
        """Evaluate `signal` against `rules`; dispatch matches that are off
        cooldown. Returns a list of delivery dicts (also persisted via
        `record` when supplied)."""
        symbol = (signal.get("symbol") or "").upper()
        now = time.time()
        out: list[dict] = []

        for rule in rules:
            if not matches(rule, signal):
                continue
            if not self._cooled_down(rule, symbol, now):
                continue
            self._last_fired[(rule.id, symbol)] = now

            payload = {
                "rule_name": rule.name,
                "symbol": symbol,
                "metric": rule.metric,
                "value": metric_value(signal, rule.metric),
                "direction": signal.get("direction"),
                "composite": signal.get("composite_score"),
                "confidence": signal.get("confidence_pct"),
            }
            title = f"ATLAS · {symbol} · {rule.name}"

            for ch_name in rule.channels:
                channel = self.channels.get(ch_name)
                if channel is None:
                    result_ok, detail = False, f"unknown channel {ch_name}"
                elif not channel.available:
                    result_ok, detail = False, f"{ch_name} unavailable"
                else:
                    res = await channel.send(title=title, body=title, payload=payload)
                    result_ok, detail = res.ok, res.detail

                mx.ALERTS_FIRED_TOTAL.labels(channel=ch_name, ok=str(result_ok).lower()).inc()
                rec = {"rule_id": rule.id, "symbol": symbol, "channel": ch_name, "ok": result_ok, "detail": detail}
                out.append(rec)
                if record is not None:
                    try:
                        await record(rule.id, symbol, ch_name, result_ok, detail, payload)
                    except Exception as e:
                        log.warning("alert.record_failed", error=str(e))

        return out
