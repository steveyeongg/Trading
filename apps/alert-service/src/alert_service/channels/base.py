"""Channel interface + shared formatter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    detail: str = ""


# ── Secret redaction ─────────────────────────────────────────────────────────
#
# httpx's HTTPStatusError message includes the full request URL, and Telegram's
# Bot API embeds the token *in the URL path* (`/bot<TOKEN>/sendMessage`). Any
# channel that does `str(exception)` without scrubbing has been leaking that
# token straight into the alert_deliveries.detail column, log lines, and the
# admin UI. This function is the single choke point every channel goes
# through before writing an error string anywhere durable.

# Telegram bot token: `bot<numeric_id>:<35+ char secret>`.
_TELEGRAM_TOKEN_RE = re.compile(r"bot(\d+):[A-Za-z0-9_-]{20,}")

# HTTP Basic-Auth headers rendered into logs by some clients.
_BASIC_AUTH_RE = re.compile(r"(?i)(authorization:\s*basic\s+)\S+")
_BEARER_AUTH_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+")

# `smtp[s]://user:password@host` and similar URL-embedded credentials.
_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)([^:@/\s]+):[^@\s]+@")


def redact_secrets(text: str) -> str:
    """Scrub known secret shapes from a free-form message string.

    Conservative — only patterns we've actually leaked in this codebase or
    are known to appear in provider error responses. Extend as new sinks
    show up. Idempotent; safe to call twice.
    """
    if not text:
        return text
    text = _TELEGRAM_TOKEN_RE.sub(r"bot\1:REDACTED", text)
    text = _BASIC_AUTH_RE.sub(r"\1REDACTED", text)
    text = _BEARER_AUTH_RE.sub(r"\1REDACTED", text)
    text = _URL_USERINFO_RE.sub(r"\1\2:REDACTED@", text)
    return text


class Channel(Protocol):
    name: str

    @property
    def available(self) -> bool:
        """False when required credentials/config are missing — the engine
        skips unavailable channels instead of erroring."""
        ...

    async def send(self, *, title: str, body: str, payload: dict[str, Any]) -> DeliveryResult:
        ...


# ── BLUEPRINT §12.3 — Telegram message format ────────────────────────────────


_DISCLAIMER = "Informational only. Not financial advice."


def _fmt(v: Any) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:.2f}"
    return str(v)


def _events_summary(events: dict[str, bool] | None) -> str:
    """Compact line listing which §12.2 triggers fired on this signal."""
    if not events:
        return ""
    fired = [name.replace("_", " ") for name, v in events.items() if v]
    if not fired:
        return ""
    return "Triggers: " + ", ".join(fired)


def format_alert(payload: dict[str, Any]) -> tuple[str, str]:
    """Render an alert payload in the §12.3 layout.

    Layout:
        🚨 ATLAS Signal: <SYM> <DIR>

        Score: <S> | Confidence: <C>% | Conviction: <K>
        Entry: <E>
        SL: <S>
        T1: <t1> | T2: <t2> | T3: <t3>
        R:R: 1:<rr>

        Why:
        - <bullet 1>
        - <bullet 2>

        Invalidation:
        <first invalidation>

        Informational only. Not financial advice.
    """
    sym = payload.get("symbol", "?")
    direction = (payload.get("direction") or "").upper() or "—"
    composite = payload.get("composite")
    conf = payload.get("confidence")
    conviction = (payload.get("conviction") or "").upper() or "—"
    entry = payload.get("entry_price")
    stop = payload.get("stop_price")
    targets = payload.get("take_profit_levels") or []
    t1 = targets[0] if len(targets) >= 1 else None
    t2 = targets[1] if len(targets) >= 2 else None
    t3 = targets[2] if len(targets) >= 3 else None
    rr = payload.get("expected_rr")
    invalidations = payload.get("invalidations") or []
    why_lines = payload.get("why_lines") or []
    events_line = _events_summary(payload.get("events"))

    title = f"🚨 ATLAS Signal: {sym} {direction}"

    lines: list[str] = [
        title,
        "",
        f"Score: {_fmt(composite)} | Confidence: {_fmt(conf)}% | Conviction: {conviction}",
        f"Entry: {_fmt(entry)}",
        f"SL: {_fmt(stop)}",
        f"T1: {_fmt(t1)} | T2: {_fmt(t2)} | T3: {_fmt(t3)}",
        f"R:R: 1:{_fmt(rr)}",
    ]
    if events_line:
        lines.append(events_line)
    if why_lines:
        lines += ["", "Why:"]
        for w in why_lines[:4]:
            lines.append(f"- {w}")
    if invalidations:
        lines += ["", "Invalidation:", invalidations[0]]
    lines += ["", _DISCLAIMER]

    body = "\n".join(lines)
    return title, body
