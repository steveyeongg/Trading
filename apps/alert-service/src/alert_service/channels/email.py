"""Email channel — SMTP. Skips if SMTP host/recipient not configured.

Uses stdlib smtplib in a threadpool (no async SMTP dep). For production scale,
swap to a provider API (SendGrid/SES) — same Channel interface.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
from email.message import EmailMessage
from typing import Any

from atlas_shared.logging import get_logger

from alert_service.channels.base import DeliveryResult, format_alert, redact_secrets

log = get_logger("alert.email")


class EmailChannel:
    name = "email"

    def __init__(self) -> None:
        self.host = os.environ.get("SMTP_HOST")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER")
        self.password = os.environ.get("SMTP_PASSWORD")
        self.sender = os.environ.get("ALERT_EMAIL_FROM", self.user or "")
        self.recipient = os.environ.get("ALERT_EMAIL_TO")

    @property
    def available(self) -> bool:
        return bool(self.host and self.recipient and self.sender)

    def _send_sync(self, title: str, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg.set_content(text)
        with smtplib.SMTP(self.host, self.port, timeout=15) as server:
            server.starttls()
            if self.user and self.password:
                server.login(self.user, self.password)
            server.send_message(msg)

    async def send(self, *, title: str, body: str, payload: dict[str, Any]) -> DeliveryResult:
        if not self.available:
            return DeliveryResult(ok=False, detail="email not configured")
        _, text = format_alert(payload)
        try:
            await asyncio.to_thread(self._send_sync, title, text)
            return DeliveryResult(ok=True, detail="sent")
        except Exception as e:
            # SMTP tracebacks can echo authentication headers on some servers.
            safe = redact_secrets(str(e))
            log.warning("alert.email.failed", error=safe)
            return DeliveryResult(ok=False, detail=safe[:200])
