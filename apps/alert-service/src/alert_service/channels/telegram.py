"""Telegram channel — Bot API sendMessage. Skips if no token/chat configured."""

from __future__ import annotations

import os
from typing import Any

import httpx
from atlas_shared.logging import get_logger

from alert_service.channels.base import DeliveryResult, format_alert

log = get_logger("alert.telegram")


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    @property
    def available(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, *, title: str, body: str, payload: dict[str, Any]) -> DeliveryResult:
        if not self.available:
            return DeliveryResult(ok=False, detail="telegram not configured")
        # BLUEPRINT §12.3 — `format_alert` already emits the full title + body
        # in the spec'd layout (emoji header, trade plan, invalidation,
        # disclaimer). Send the body verbatim; the title is the first line.
        _, text = format_alert(payload)
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json={"chat_id": self.chat_id, "text": text})
                r.raise_for_status()
            return DeliveryResult(ok=True, detail="sent")
        except Exception as e:
            log.warning("alert.telegram.failed", error=str(e))
            return DeliveryResult(ok=False, detail=str(e)[:200])
