"""Regression guard for secret leaks in alert delivery details + logs.

Pinned incident: an httpx `HTTPStatusError` from Telegram's `sendMessage`
endpoint embeds the full request URL in the exception's `__str__`, and the
URL contains the bot token verbatim:

    Client error '403 Forbidden' for url
    'https://api.telegram.org/bot123456789:AAFakeToken…/sendMessage'

The old code stored that string in `alert_deliveries.detail` and logged it,
leaking the token to anyone with DB read access or log tail.

The fix is a single choke point — `redact_secrets` in
`alert_service.channels.base` — that every channel routes exception messages
through before persisting or logging. These tests pin the shape of that
redaction so it can't silently regress.
"""

from __future__ import annotations

import pytest
from alert_service.channels.base import redact_secrets

# ── Telegram bot token — the actual leak we shipped ──────────────────────────


def test_telegram_bot_token_in_url_is_redacted() -> None:
    """The exact shape of the leak that happened in production."""
    leaked = (
        "Client error '403 Forbidden' for url "
        "'https://api.telegram.org/bot123456789:AAFakeTokenForTestsOnly_0123456789ab/sendMessage'\n"
        "For more information check: https://developer.mozilla.org/…"
    )
    safe = redact_secrets(leaked)
    # Bot ID may stay (it's not secret; you can already read it from getMe).
    assert "bot123456789:REDACTED" in safe
    # The secret suffix MUST be gone.
    assert "AAFakeTokenForTestsOnly_0123456789ab" not in safe
    # Rest of the message stays intact so operators can still diagnose.
    assert "403 Forbidden" in safe
    assert "sendMessage" in safe


def test_short_alphanumeric_is_not_mistaken_for_a_token() -> None:
    """The Telegram token pattern requires 20+ chars of secret after the
    colon, so a random `bot123:abc/foo` in someone's payload is left alone."""
    assert redact_secrets("bot123:abc/foo") == "bot123:abc/foo"


def test_multiple_tokens_in_one_string_all_redacted() -> None:
    a = "bot111:AAABBBCCCDDDEEEFFFGGG_HHH_IIIJJJKKK"
    b = "bot222:MMMNNNPPPQQQRRRSSSTTT-UUUVVVWWWXXX"
    text = f"first {a} then {b}"
    safe = redact_secrets(text)
    assert "AAABBBCCCDDDEEEFFFGGG_HHH_IIIJJJKKK" not in safe
    assert "MMMNNNPPPQQQRRRSSSTTT-UUUVVVWWWXXX" not in safe
    assert safe.count("REDACTED") == 2


# ── HTTP auth headers ─────────────────────────────────────────────────────────


def test_basic_auth_header_redacted() -> None:
    txt = "Request headers: Authorization: Basic dXNlcjpwYXNzd29yZA=="
    safe = redact_secrets(txt)
    assert "dXNlcjpwYXNzd29yZA==" not in safe
    assert "Basic REDACTED" in safe


def test_bearer_token_header_redacted() -> None:
    txt = "auth failed. authorization: bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0"
    safe = redact_secrets(txt)
    assert "eyJhbGciOiJIUzI1NiJ9" not in safe
    # The header prefix keeps original case; REDACTED is always uppercase.
    assert "bearer REDACTED" in safe


# ── URL-embedded credentials (webhook / SMTP URLs) ────────────────────────────


def test_url_user_password_redacted() -> None:
    txt = "connection to https://alerts_user:sup3rSecret@hooks.example.com/webhook failed"
    safe = redact_secrets(txt)
    assert "sup3rSecret" not in safe
    assert "https://alerts_user:REDACTED@hooks.example.com/webhook" in safe


def test_smtp_url_credentials_redacted() -> None:
    txt = "SMTP connection error: smtps://svc:P@ss123!@smtp.example.com:465 timed out"
    safe = redact_secrets(txt)
    assert "P@ss123!" not in safe
    assert "svc:REDACTED@" in safe


# ── Safety properties ────────────────────────────────────────────────────────


def test_empty_and_none_safe() -> None:
    """Callers always pass `str(e)` (never None), so the function only needs
    to guarantee it doesn't blow up on empty strings."""
    assert redact_secrets("") == ""
    # Falsy guard covers None too — returns it unchanged rather than crashing.
    assert redact_secrets(None) is None  # type: ignore[arg-type]


def test_idempotent() -> None:
    txt = "Client error for url https://api.telegram.org/bot42:ABCDEFGHIJKLMNOPQRSTUVWX/sendMessage"
    once = redact_secrets(txt)
    twice = redact_secrets(once)
    assert once == twice


def test_non_secret_text_unchanged() -> None:
    """Regular error strings should not be mangled — false positives here
    are worse than no redaction, since operators lose diagnostic signal."""
    txt = "no bars stored for TSLA @ 1m; ingest_equities backfill required"
    assert redact_secrets(txt) == txt


# ── End-to-end via TelegramChannel.send ──────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_channel_delivery_detail_never_contains_token() -> None:
    """Real exception path: send() hits a mock endpoint that answers 403, so
    `raise_for_status` raises an HTTPStatusError whose message carries the
    token in the URL. The returned `detail` must be scrubbed."""
    import httpx
    from alert_service.channels.telegram import TelegramChannel

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden: bot is not authorized")

    # A believable-shape token so the redaction regex triggers.
    fake_token = "9999999999:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ch = TelegramChannel(
        token=fake_token, chat_id="12345", transport=httpx.MockTransport(handler)
    )
    res = await ch.send(title="t", body="t", payload={"symbol": "AAPL"})

    assert res.ok is False
    assert "403" in res.detail, "the failure must reach detail, or this test proves nothing"
    assert fake_token.split(":")[1] not in res.detail, "token secret leaked into detail"
    assert "bot9999999999:REDACTED" in res.detail
