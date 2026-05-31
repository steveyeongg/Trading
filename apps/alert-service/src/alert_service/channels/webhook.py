"""Outgoing webhook channel — HMAC-SHA256 signed POST (BLUEPRINT §6.5)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

import httpx
from atlas_shared.logging import get_logger

from alert_service.channels.base import DeliveryResult

log = get_logger("alert.webhook")


class WebhookChannel:
    name = "webhook"

    def __init__(self, url: str | None = None, secret: str | None = None):
        self.url = url or os.environ.get("ALERT_WEBHOOK_URL")
        self.secret = secret or os.environ.get("ALERT_WEBHOOK_SECRET", "")

    @property
    def available(self) -> bool:
        return bool(self.url)

    def _sign(self, body: bytes) -> str:
        return hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()

    async def send(self, *, title: str, body: str, payload: dict[str, Any]) -> DeliveryResult:
        if not self.available:
            return DeliveryResult(ok=False, detail="no webhook url")
        envelope = {
            "type": "alert.fired",
            "ts": int(time.time()),
            "title": title,
            "alert": payload,
        }
        raw = json.dumps(envelope, default=str).encode()
        headers = {
            "content-type": "application/json",
            "x-atlas-signature": self._sign(raw),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(self.url, content=raw, headers=headers)
                r.raise_for_status()
            return DeliveryResult(ok=True, detail=f"http {r.status_code}")
        except Exception as e:
            log.warning("alert.webhook.failed", error=str(e))
            return DeliveryResult(ok=False, detail=str(e)[:200])
