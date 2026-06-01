"""Alert engine: evaluate a signal against rules, derive §12.2 events,
respect cooldown, dispatch via channels.

State carried between calls (per process):
  - `_last_fired`        : per (rule, symbol) cooldown.
  - `_last_signal`       : last published Signal dict per symbol — used by
                           `events.derive_events` to spot upgrades / new
                           direction / price-level crossings.
  - `_last_regime`       : last regime label per symbol so we can detect
                           macro regime changes.
"""

from __future__ import annotations

import time
from typing import Any

from atlas_shared import metrics as mx
from atlas_shared.logging import get_logger

from alert_service.channels import build_channels
from alert_service.events import derive_events
from alert_service.rules import AlertRule, matches, metric_value

log = get_logger("alert.engine")


def _why_lines(signal: dict[str, Any]) -> list[str]:
    """Render two-to-four short "why" bullets from sub_scores for the Telegram
    body. Skips zero contributions; orders by absolute contribution size."""
    subs = signal.get("sub_scores") or {}
    labels = [
        ("EMA stack & trend block", subs.get("tech", 0.0)),
        ("Quant trend model", subs.get("quant", 0.0)),
        ("News flow", subs.get("news", 0.0)),
        ("Macro regime", subs.get("macro", 0.0)),
        ("Risk pre-screen", subs.get("risk", 0.0)),
    ]
    ranked = sorted([(n, v) for n, v in labels if abs(v) >= 5.0], key=lambda kv: abs(kv[1]), reverse=True)
    return [f"{n}: {v:+.0f}" for n, v in ranked[:4]]


class AlertEngine:
    def __init__(self) -> None:
        self.channels = build_channels()
        self._last_fired: dict[tuple[str, str], float] = {}
        self._last_signal: dict[str, dict[str, Any]] = {}
        self._last_regime: dict[str, str] = {}

    def _cooled_down(self, rule: AlertRule, symbol: str, now: float) -> bool:
        last = self._last_fired.get((rule.id, symbol))
        return last is None or (now - last) >= rule.cooldown_s

    async def evaluate_signal(
        self,
        signal: dict[str, Any] | None,
        rules: list[AlertRule],
        *,
        last_price: float | None = None,
        regime: str | None = None,
        record=None,
    ) -> list[dict]:
        """Evaluate against rules; dispatch matches that are off cooldown.

        `signal` may be None (the gates killed the candidate) — we still
        derive events so rules listening for `risk_veto_changed` or
        `macro_regime_changed` fire correctly."""
        symbol = ((signal or {}).get("symbol") or "").upper()
        if not symbol:
            return []
        now = time.time()
        out: list[dict] = []

        prior = self._last_signal.get(symbol)
        prior_regime = self._last_regime.get(symbol)
        events = derive_events(
            current=signal,
            last_price=last_price,
            prior_signal=prior,
            prior_regime=prior_regime,
            current_regime=regime,
        )

        # Persist new state. Only update last_signal when we have one — keeps
        # the prior alive while the trade is closed/vetoed.
        if signal is not None:
            self._last_signal[symbol] = signal
        if regime is not None:
            self._last_regime[symbol] = regime

        if signal is None:
            # Without a signal there's nothing concrete to fire on except the
            # state-change events (veto/regime). The existing scalar-rule path
            # needs a signal payload, so for now we just exit; future work
            # can expose state-change-only rules.
            return out

        # BLUEPRINT §12.3 payload — carries the full trade plan, derived
        # events, and a "why" line list so the channel formatters can render
        # the spec'd layout without re-deriving anything.
        payload_template = {
            "symbol": symbol,
            "direction": signal.get("direction"),
            "composite": signal.get("composite_score"),
            "confidence": signal.get("confidence_pct"),
            "conviction": signal.get("conviction"),
            "entry_price": signal.get("entry_price"),
            "stop_price": signal.get("stop_price"),
            "take_profit_levels": signal.get("take_profit_levels"),
            "expected_rr": signal.get("expected_rr"),
            "invalidations": signal.get("invalidations") or [],
            "why_lines": _why_lines(signal),
            "events": events.as_dict(),
        }

        for rule in rules:
            if not matches(rule, signal):
                continue
            if not self._cooled_down(rule, symbol, now):
                continue
            self._last_fired[(rule.id, symbol)] = now

            payload = {
                **payload_template,
                "rule_name": rule.name,
                "metric": rule.metric,
                "value": metric_value(signal, rule.metric),
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
