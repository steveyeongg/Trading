"""Log channel — always available, writes the alert to structured logs.

The zero-config default so the alert pipeline is demonstrable offline.
"""

from __future__ import annotations

from typing import Any

from atlas_shared.logging import get_logger

from alert_service.channels.base import DeliveryResult, format_alert

log = get_logger("alert.log")


class LogChannel:
    name = "log"

    @property
    def available(self) -> bool:
        return True

    async def send(self, *, title: str, body: str, payload: dict[str, Any]) -> DeliveryResult:
        _, text = format_alert(payload)
        log.info("alert.fired", channel="log", title=title, body=text.replace("\n", " | "))
        return DeliveryResult(ok=True, detail="logged")
