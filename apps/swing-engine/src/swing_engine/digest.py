"""The nightly Telegram digest: tonight's validated ideas, the market, and the user's positions.

The text is a fixed template filled with the scan's own numbers; no model writes any of it, and it
promises nothing. What it will and will not list:

* **Validated setups only.** An idea is listed only when ``idea.live`` is True (its setup passed
  out-of-sample testing). Unproven ideas are never listed, and neither are watch-only names; the
  digest says how many unproven ideas wait on the dashboard when there is nothing validated.
* **At most five**, best rank first, numbered 1 to 5 in the order printed (the idea's own rank can
  have gaps once held names and unproven setups are taken out).
* **Never a stock the user already holds**, taken out before the cut to five.
* **Nothing to buy in a downtrend.** The market line says to stand aside, and listing a buy line
  under it would contradict it, so no idea is printed even if an ungated validated setup produced
  one (it is still on the dashboard).

Telegram refuses a message over 4,096 characters. The idea list (five) and the missed-session list
(ten) are capped, and if the positions still push the text past :data:`MAX_CHARS` the last
position lines are replaced by one ``+N more`` line, so the digest always fits and always ends with
the disclaimer.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from datetime import date

from alert_service.channels.base import DeliveryResult
from alert_service.channels.telegram import TelegramChannel

from swing_engine.narrate import SETUP_LABEL, day_label
from swing_engine.positions import Action
from swing_engine.regime import Regime
from swing_engine.scan import Idea, ScanResult

MAX_IDEAS = 5
"""Most ideas listed in one digest."""

MAX_MISSED = 10
"""Most missed sessions named in one digest (the most recent ones; the rest are counted)."""

MAX_CHARS = 4000
"""The digest's length budget, a margin under Telegram's 4,096-character message limit."""

DISCLAIMER = "Informational only, not financial advice. You place every order yourself."

REGIME_LABEL = {
    Regime.UPTREND: "UPTREND, new trades OK",
    Regime.MIXED: "MIXED, half-size trades",
    Regime.DOWNTREND: "DOWNTREND, stand aside: no new buy ideas",
}


def format_digest(
    scan: ScanResult,
    target_session: date,
    actions: Sequence[Action],
    missed: Sequence[date],
    unproven_count: int,
    held: Collection[str] = (),
) -> str:
    """The Telegram text for one scan.

    ``target_session`` is the session the buy-stops are live for. ``actions`` are the nightly
    actions for the user's open positions, ``missed`` the sessions caught up by this run,
    ``unproven_count`` how many unproven ideas the scan found (for the nothing-validated line) and
    ``held`` the symbols of the user's open positions, which are not listed as new ideas.
    """
    head = [f"ATLAS · ideas for {day_label(target_session)}", _market_line(scan)]
    if missed:
        head.append(_missed_line(missed))
    head.extend(_ideas_block(scan, held, unproven_count))

    rows = [f"• {a.symbol} {a.text}" for a in actions]
    text = _assemble(head, rows)
    for keep in range(len(rows) - 1, -1, -1):
        if len(text) <= MAX_CHARS:
            break
        text = _assemble(head, [*rows[:keep], f"• +{len(rows) - keep} more on the dashboard"])
    return text


def format_failure(session: date, step: str, reason: str) -> str:
    """The one-line message sent instead of a digest when the scan failed at ``step``."""
    return f"ATLAS scan failed at step {step}: {reason}"


async def send_digest(text: str, channel: TelegramChannel | None = None) -> DeliveryResult:
    """Send ``text`` to Telegram. Never raises: an unconfigured or failing channel is a result.

    ``channel`` defaults to a :class:`TelegramChannel` reading ``TELEGRAM_BOT_TOKEN`` and
    ``TELEGRAM_CHAT_ID`` from the environment; without them the result is ``ok=False`` with
    ``telegram not configured``.
    """
    return await (channel if channel is not None else TelegramChannel()).send_text(text)


def _assemble(head: Sequence[str], position_rows: Sequence[str]) -> str:
    lines = list(head)
    if position_rows:
        lines.append("Your positions:")
        lines.extend(position_rows)
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _market_line(scan: ScanResult) -> str:
    line = f"Market: {REGIME_LABEL[Regime(scan.regime)]}"
    if math.isfinite(scan.breadth):
        line += f" · breadth {scan.breadth:.0f}%"
    return line


def _missed_line(missed: Sequence[date]) -> str:
    shown = list(missed)[-MAX_MISSED:]
    line = "Missed sessions caught up: " + ", ".join(day_label(d) for d in shown)
    if len(missed) > len(shown):
        line += f" (+{len(missed) - len(shown)} earlier)"
    return line


def _ideas_block(scan: ScanResult, held: Collection[str], unproven_count: int) -> list[str]:
    if Regime(scan.regime) is Regime.DOWNTREND:
        return []  # the market line already says to stand aside
    live = [i for i in scan.ideas if i.live]
    owned = {s.upper() for s in held}
    fresh = sorted(
        (i for i in live if i.symbol.upper() not in owned),
        key=lambda i: (i.rank, i.symbol, i.setup),
    )[:MAX_IDEAS]
    if fresh:
        return [
            "Top ideas (validated setups):",
            *(f"{n}. {_idea_line(i)}" for n, i in enumerate(fresh, start=1)),
        ]
    if live:
        return ["Tonight's validated ideas are all stocks you already hold."]
    if unproven_count > 0:
        noun = "idea" if unproven_count == 1 else "ideas"
        return [f"No validated setups yet; {unproven_count} unproven {noun} on the dashboard"]
    return [f"No stock met a setup's rules after {day_label(scan.session)}'s close."]


def _idea_line(idea: Idea) -> str:
    label = SETUP_LABEL.get(idea.setup, idea.setup.capitalize())
    sizing = idea.sizing
    return (
        f"{idea.symbol} {label} · buy above {idea.trigger:.2f} · stop {idea.stop:.2f}"
        f" · {sizing.shares} sh · risk ${sizing.risk_dollars:.0f}"
    )
