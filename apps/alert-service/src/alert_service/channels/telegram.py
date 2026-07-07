"""Telegram channel — Bot API sendMessage. Skips if no token/chat configured."""

from __future__ import annotations

import os
from typing import Any

import httpx
from atlas_shared.logging import get_logger

from alert_service.channels.base import DeliveryResult, format_alert, redact_secrets

log = get_logger("alert.telegram")


class TelegramChannel:
    """Telegram Bot API sender.

    Env is resolved on each `available` check and each `send` call — not
    captured at construction — because the AlertEngine (and thus this
    channel) is built at module import time, before the service lifespan has
    a chance to call ``atlas_shared.load_env()`` to mirror ``.env`` into
    ``os.environ``. Capturing at init would freeze empty credentials and
    keep reporting "not configured" long after keys were loaded.
    """

    name = "telegram"

    def __init__(self, token: str | None = None, chat_id: str | None = None):
        # Overrides take precedence — used by tests to inject fake creds.
        self._token_override = token
        self._chat_override = chat_id

    def _token(self) -> str | None:
        return self._token_override or os.environ.get("TELEGRAM_BOT_TOKEN")

    def _chat_id(self) -> str | None:
        return self._chat_override or os.environ.get("TELEGRAM_CHAT_ID")

    @property
    def token(self) -> str | None:      # kept for backward-compat readers
        return self._token()

    @property
    def chat_id(self) -> str | None:
        return self._chat_id()

    @property
    def available(self) -> bool:
        return bool(self._token() and self._chat_id())

    async def send(self, *, title: str, body: str, payload: dict[str, Any]) -> DeliveryResult:
        token, chat_id = self._token(), self._chat_id()
        if not (token and chat_id):
            return DeliveryResult(ok=False, detail="telegram not configured")
        # BLUEPRINT §12.3 — `format_alert` already emits the full title + body
        # in the spec'd layout (emoji header, trade plan, invalidation,
        # disclaimer). Send the body verbatim; the title is the first line.
        _, text = format_alert(payload)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json={"chat_id": chat_id, "text": text})
                r.raise_for_status()
            return DeliveryResult(ok=True, detail="sent")
        except Exception as e:
            # httpx exception messages embed the full request URL, which for
            # Telegram contains the bot token verbatim. redact_secrets rewrites
            # `bot<id>:<token>` → `bot<id>:REDACTED` before it lands anywhere
            # durable (logs, delivery detail column, admin UI).
            safe = redact_secrets(str(e))
            log.warning("alert.telegram.failed", error=safe)
            return DeliveryResult(ok=False, detail=safe[:200])
