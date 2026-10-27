"""Swing engine: the nightly Telegram digest and the channel's plain-text send.

The digest lists only validated (``live``) ideas, the top five by rank, never one the user already
holds and never a watch-only name. A scan with no validated idea says so, and a downtrend says to
stand aside instead of listing anything to buy. ``send_text`` is the digest's way out through the
existing Telegram channel and has to keep its token-redaction guarantee: httpx puts the full
request URL, bot token included, in its error messages.

No test here touches the network or the real ``.env`` credentials: the transport is an
``httpx.MockTransport`` and every token and chat id is a fake passed in explicitly.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest
from alert_service.channels.base import DeliveryResult
from alert_service.channels.telegram import TelegramChannel
from swing_engine.digest import format_digest, format_failure, send_digest
from swing_engine.positions import Action, ActionKind
from swing_engine.regime import Regime
from swing_engine.scan import Idea, ScanResult
from swing_engine.sizing import Capacity, Sizing

TARGET = date(2026, 10, 19)  # Mon 19 Oct

# Shaped like a real bot token so redact_secrets' pattern applies to it.
FAKE_TOKEN = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FAKE_SECRET = FAKE_TOKEN.split(":")[1]
FAKE_CHAT = "424242"

DISCLAIMER = "Informational only, not financial advice. You place every order yourself."


def _idea(
    symbol: str,
    setup: str = "breakout",
    *,
    rank: int = 1,
    trigger: float = 148.2,
    stop: float = 140.1,
    shares: int = 30,
    risk: float = 243.0,
    live: bool = True,
    regime_warning: bool = False,
) -> Idea:
    return Idea(
        symbol=symbol,
        setup=setup,
        version=f"{setup}-v1",
        trigger=trigger,
        stop=stop,
        atr14=4.05,
        rs_pct=97.4,
        rank=rank,
        sizing=Sizing(
            shares=shares, risk_dollars=risk, position_value=shares * trigger, capped_by=None
        ),
        overlaps=[],
        live=live,
        regime_warning=regime_warning,
    )


def _xyz() -> Idea:
    return _idea("XYZ", "breakout", rank=1)


def _abc() -> Idea:
    return _idea("ABC", "pullback", rank=2, trigger=72.15, stop=69.30, shares=87, risk=247.95)


def _scan(
    ideas: list[Idea],
    *,
    watch_only: list[Idea] | None = None,
    regime: Regime = Regime.UPTREND,
    breadth: float = 61.2,
) -> ScanResult:
    return ScanResult(
        session=date(2026, 10, 16),
        regime=regime,
        regime_factor={Regime.UPTREND: 1.0, Regime.MIXED: 0.5, Regime.DOWNTREND: 0.0}[regime],
        breadth=breadth,
        universe_size=1500,
        ideas=ideas,
        watch_only=watch_only or [],
        capacity=Capacity(
            open_positions=0, max_positions=8, open_risk_dollars=0.0, max_risk_dollars=1500.0
        ),
    )


def _action(symbol: str, text: str, kind: ActionKind = ActionKind.HOLD) -> Action:
    return Action(position_id=f"id-{symbol}", symbol=symbol, kind=kind, stop=None, text=text)


RAISE_MSFT = _action("MSFT", "raise stop to 512.40 (trail)", ActionKind.RAISE_STOP)


# ------------------------------------------------------------------------------ the digest


def test_digest_golden():
    scan = _scan([_xyz(), _abc(), _idea("UNP", rank=3, live=False)])

    assert format_digest(scan, TARGET, [RAISE_MSFT], [], 1) == (
        "ATLAS · ideas for Mon 19 Oct\n"
        "Market: UPTREND, new trades OK · breadth 61%\n"
        "Top ideas (validated setups):\n"
        "1. XYZ Breakout · buy above 148.20 · stop 140.10 · 30 sh · risk $243\n"
        "2. ABC Pullback · buy above 72.15 · stop 69.30 · 87 sh · risk $248\n"
        "Your positions:\n"
        "• MSFT raise stop to 512.40 (trail)\n"
        "Informational only, not financial advice. You place every order yourself."
    )


def test_digest_without_live_setups_lists_no_unproven_ideas():
    scan = _scan([_idea("UNP1", rank=1, live=False), _idea("UNP2", rank=2, live=False)])

    text = format_digest(scan, TARGET, [], [], 6)

    assert "No validated setups yet; 6 unproven ideas on the dashboard" in text
    assert "buy above" not in text
    assert "UNP1" not in text and "UNP2" not in text
    assert text.endswith(DISCLAIMER)


def test_one_unproven_idea_reads_as_singular():
    text = format_digest(_scan([_idea("UNP", live=False)]), TARGET, [], [], 1)

    assert "No validated setups yet; 1 unproven idea on the dashboard" in text


def test_digest_with_no_ideas_at_all_says_nothing_met_the_rules():
    text = format_digest(_scan([]), TARGET, [], [], 0)

    assert "No stock met a setup's rules after Fri 16 Oct's close." in text
    assert "No validated setups yet" not in text
    assert "buy above" not in text


def test_digest_lists_missed_sessions():
    scan = _scan([_xyz()])

    text = format_digest(scan, TARGET, [], [date(2026, 10, 15), date(2026, 10, 16)], 0)

    assert "Missed sessions caught up: Thu 15 Oct, Fri 16 Oct" in text


def test_digest_omits_missed_line_and_positions_section_when_empty():
    text = format_digest(_scan([_xyz()]), TARGET, [], [], 0)

    assert "Missed sessions" not in text
    assert "Your positions" not in text


def test_downtrend_digest_says_stand_aside():
    # An ungated live setup keeps its ideas in `ideas` in a downtrend; the digest still stands aside.
    scan = _scan(
        [_xyz()],
        watch_only=[_idea("WATCH", rank=1)],
        regime=Regime.DOWNTREND,
        breadth=22.0,
    )

    text = format_digest(scan, TARGET, [], [], 2)

    assert "Market: DOWNTREND, stand aside: no new buy ideas · breadth 22%" in text
    assert "buy above" not in text
    assert "XYZ" not in text and "WATCH" not in text
    assert text.endswith(DISCLAIMER)


def test_mixed_regime_line():
    text = format_digest(_scan([_xyz()], regime=Regime.MIXED, breadth=48.6), TARGET, [], [], 0)

    assert "Market: MIXED, half-size trades · breadth 49%" in text


def test_nan_breadth_drops_the_breadth_suffix():
    text = format_digest(_scan([_xyz()], breadth=float("nan")), TARGET, [], [], 0)

    assert "Market: UPTREND, new trades OK\n" in text
    assert "breadth" not in text
    assert "nan%" not in text.lower()  # not bare "nan": "financial" contains it


def test_digest_never_lists_watch_only_ideas():
    scan = _scan([_xyz()], watch_only=[_idea("WATCH", rank=1)])

    text = format_digest(scan, TARGET, [], [], 0)

    assert "XYZ" in text and "WATCH" not in text


def test_digest_excludes_held_symbols_before_the_top_five_cut():
    ideas = [_idea(s, rank=n) for n, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"], 1)]

    text = format_digest(_scan(ideas), TARGET, [], [], 0, held={"aaa", "BBB"})

    assert "AAA" not in text and "BBB" not in text
    # Held names do not use up a slot: the five that remain are CCC..FFF, four of them.
    assert "1. CCC" in text and "2. DDD" in text and "3. EEE" in text and "4. FFF" in text
    assert "5." not in text


def test_digest_with_every_live_idea_held_lists_none():
    text = format_digest(_scan([_xyz()]), TARGET, [], [], 0, held=["XYZ"])

    assert "XYZ" not in text and "buy above" not in text
    assert "Top ideas" not in text
    assert "Tonight's validated ideas are all stocks you already hold." in text


def test_zero_share_idea_is_not_listed_and_says_it_does_not_fit_the_risk_settings():
    # The incident: a $912 stock at half size sizes to 0 shares and printed
    # "buy above 912.00 · stop 850.00 · 0 sh · risk $0", which reads as risk-free.
    nvda = _idea("NVDA", rank=1, trigger=912.0, stop=850.0, shares=0, risk=0.0)

    text = format_digest(_scan([nvda], regime=Regime.MIXED), TARGET, [], [], 0)

    assert "NVDA" not in text and "buy above" not in text and "0 sh" not in text
    assert "Top ideas" not in text
    assert "Tonight's validated ideas don't fit your risk settings; see the dashboard." in text
    assert text.endswith(DISCLAIMER)


def test_zero_share_idea_does_not_take_a_slot_from_a_placeable_one():
    ideas = [_idea("ZERO", rank=1, shares=0, risk=0.0)] + [
        _idea(sym, rank=n) for n, sym in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"], start=2)
    ]

    text = format_digest(_scan(ideas), TARGET, [], [], 0)

    assert "ZERO" not in text
    lines = [line for line in text.splitlines() if "buy above" in line]
    assert [line.split(" ")[:2] for line in lines] == [
        ["1.", "AAA"], ["2.", "BBB"], ["3.", "CCC"], ["4.", "DDD"], ["5.", "EEE"]
    ]  # fmt: skip
    assert "don't fit your risk settings" not in text


def test_held_ideas_and_zero_share_ideas_together_say_risk_settings_not_all_held():
    ideas = [_idea("HELD", rank=1), _idea("ZERO", rank=2, shares=0, risk=0.0)]

    text = format_digest(_scan(ideas), TARGET, [], [], 0, held=["HELD"])

    assert "don't fit your risk settings" in text
    assert "all stocks you already hold" not in text


def test_no_validated_idea_tonight_wording_when_a_validated_setup_exists():
    scan = _scan([_idea("UNP1", rank=1, live=False), _idea("UNP2", rank=2, live=False)])

    text = format_digest(scan, TARGET, [], [], 6, any_live_setup=True)

    assert "No validated-setup ideas tonight; 6 unproven ideas on the dashboard" in text
    assert "No validated setups yet" not in text
    assert "buy above" not in text


def test_no_validated_idea_tonight_wording_is_singular_for_one():
    text = format_digest(_scan([_idea("UNP", live=False)]), TARGET, [], [], 1, any_live_setup=True)

    assert "No validated-setup ideas tonight; 1 unproven idea on the dashboard" in text


def test_any_live_setup_with_nothing_unproven_says_nothing_met_the_rules():
    text = format_digest(_scan([]), TARGET, [], [], 0, any_live_setup=True)

    assert "No stock met a setup's rules after Fri 16 Oct's close." in text
    assert "validated" not in text


def test_any_live_setup_does_not_change_a_digest_that_has_ideas():
    scan = _scan([_xyz(), _abc()])

    assert format_digest(scan, TARGET, [RAISE_MSFT], [], 1, any_live_setup=True) == format_digest(
        scan, TARGET, [RAISE_MSFT], [], 1
    )


def test_regime_warning_idea_is_tagged_and_others_are_not():
    ideas = [
        _idea("WARN", rank=1, regime_warning=True),
        _idea("CALM", rank=2),
    ]

    text = format_digest(_scan(ideas, regime=Regime.MIXED), TARGET, [], [], 0)

    lines = {line.split(" ")[1]: line for line in text.splitlines() if "buy above" in line}
    assert lines["WARN"].endswith("· risk $243 · regime warning")
    assert "regime warning" not in lines["CALM"]
    assert text.count("regime warning") == 1


def test_symbols_in_position_actions_are_not_listed_as_new_ideas():
    # `held` was not passed: the open positions' actions alone keep a held stock out of the list.
    ideas = [_idea("MSFT", rank=1), _idea("AAA", rank=2)]
    actions = [RAISE_MSFT, _action("aaa", "hold, stop 10.00")]

    text = format_digest(_scan(ideas), TARGET, actions, [], 0)

    assert "buy above" not in text
    assert "1. MSFT" not in text and "AAA Breakout" not in text
    assert "Tonight's validated ideas are all stocks you already hold." in text
    assert "• MSFT raise stop to 512.40 (trail)" in text  # the positions section is untouched


def test_action_symbol_does_not_use_up_a_slot():
    ideas = [_idea(s, rank=n) for n, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"], 1)]

    text = format_digest(_scan(ideas), TARGET, [_action("bbb", "hold, stop 1.00")], [], 0)

    lines = [line for line in text.splitlines() if "buy above" in line]
    assert [line.split(" ")[1] for line in lines] == ["AAA", "CCC", "DDD", "EEE", "FFF"]


def test_a_stock_flagged_by_two_setups_is_listed_once_under_its_best_rank():
    ideas = [
        _idea("AAA", "breakout", rank=1),
        _idea("AAA", "pullback", rank=2, trigger=72.15, stop=69.30),
        _idea("BBB", rank=3),
        _idea("CCC", rank=4),
        _idea("DDD", rank=5),
        _idea("EEE", rank=6),
    ]

    text = format_digest(_scan(ideas), TARGET, [], [], 0)

    lines = [line for line in text.splitlines() if "buy above" in line]
    # Deduped before the cut, so the sixth idea (five distinct stocks) is not pushed out.
    assert [line.split(" ")[:2] for line in lines] == [
        ["1.", "AAA"], ["2.", "BBB"], ["3.", "CCC"], ["4.", "DDD"], ["5.", "EEE"]
    ]  # fmt: skip
    assert "AAA Breakout" in text and "AAA Pullback" not in text


def test_dedupe_falls_to_the_placeable_idea_when_the_best_ranked_has_no_shares():
    ideas = [
        _idea("AAA", "breakout", rank=1, shares=0, risk=0.0),
        _idea("AAA", "pullback", rank=2, trigger=72.15, stop=69.30, shares=87, risk=247.95),
    ]

    text = format_digest(_scan(ideas), TARGET, [], [], 0)

    assert "1. AAA Pullback · buy above 72.15" in text
    assert "AAA Breakout" not in text


def test_digest_caps_at_five_ideas_by_rank_and_numbers_them_in_output_order():
    # Handed over out of order, with gaps in the internal rank: output is rank order, numbered 1..5.
    ranks = {"G": 9, "A": 2, "F": 7, "C": 4, "B": 3, "E": 6, "D": 5}
    ideas = [_idea(sym, rank=rank) for sym, rank in ranks.items()]

    text = format_digest(_scan(ideas), TARGET, [], [], 0)

    lines = [line for line in text.splitlines() if "buy above" in line]
    assert [line.split(" ")[:2] for line in lines] == [
        ["1.", "A"], ["2.", "B"], ["3.", "C"], ["4.", "D"], ["5.", "E"]
    ]  # fmt: skip
    assert "F Breakout" not in text and "G Breakout" not in text


def test_digest_lists_one_line_per_position_action():
    actions = [
        RAISE_MSFT,
        _action("AAPL", "hold, stop 200.00"),
        _action("TSLA", "stop hit: exit", ActionKind.STOP_HIT),
    ]

    text = format_digest(_scan([_xyz()]), TARGET, actions, [], 0)

    assert (
        "Your positions:\n"
        "• MSFT raise stop to 512.40 (trail)\n"
        "• AAPL hold, stop 200.00\n"
        "• TSLA stop hit: exit\n"
    ) in text


def test_digest_ends_with_the_disclaimer_whatever_else_it_has():
    scan = _scan([_xyz(), _abc()])

    full = format_digest(scan, TARGET, [RAISE_MSFT], [date(2026, 10, 16)], 3)
    bare = format_digest(_scan([], regime=Regime.DOWNTREND), TARGET, [], [], 0)

    assert full.endswith(DISCLAIMER) and bare.endswith(DISCLAIMER)


def test_digest_fits_telegrams_4096_character_limit_in_the_worst_case():
    ideas = [
        _idea(f"SYM{n}", "pullback", rank=n, trigger=1234.56, stop=1111.11, shares=999)
        for n in range(1, 8)
    ]
    long_text = "no price data for POS since 2026-10-02; check the broker for a halt or a delisting"
    actions = [_action(f"POS{n:02d}", long_text, ActionKind.NO_DATA) for n in range(60)]
    # More missed sessions than there are weekdays in two months.
    missed = [date(2026, 8, 3) + timedelta(days=n) for n in range(60)]
    assert sum(len(f"• {a.symbol} {a.text}") + 1 for a in actions) > 4096  # trimming is needed

    text = format_digest(_scan(ideas, breadth=100.0), TARGET, actions, missed, 40)

    assert len(text) < 4096
    assert text.endswith(DISCLAIMER)
    assert text.count("buy above") == 5
    assert "Missed sessions caught up" in text and "(+50 earlier)" in text
    # What was cut is said, not silently dropped, and the count adds up.
    kept = text.count("• POS")
    assert 0 < kept < 60
    assert f"• +{60 - kept} more on the dashboard" in text


def test_digest_with_many_realistic_actions_still_fits():
    actions = [_action(f"POS{n:02d}", "hold, stop 123.45") for n in range(60)]

    text = format_digest(_scan([_xyz()]), TARGET, actions, [], 0)

    assert len(text) < 4096
    assert text.count("• POS") == 60 and "more on the dashboard" not in text


def test_digest_that_fits_is_not_trimmed():
    actions = [_action(f"P{n}", "hold, stop 10.00") for n in range(8)]

    text = format_digest(_scan([_xyz()]), TARGET, actions, [], 0)

    assert text.count("• P") == 8
    assert "more" not in text


def test_digest_never_uses_confidence_or_promise_language():
    text = format_digest(_scan([_xyz(), _abc()]), TARGET, [RAISE_MSFT], [date(2026, 10, 16)], 1)

    lowered = text.lower()
    for word in ("confidence", "probability", "guarantee", "will profit", "profit"):
        assert word not in lowered


def test_format_failure():
    assert (
        format_failure(date(2026, 10, 19), "ingest", "Alpaca returned 429")
        == "ATLAS scan failed at step ingest: Alpaca returned 429"
    )


# ------------------------------------------------------------------- Telegram send_text


def _channel(handler) -> TelegramChannel:
    return TelegramChannel(
        token=FAKE_TOKEN, chat_id=FAKE_CHAT, transport=httpx.MockTransport(handler)
    )


async def test_send_text_posts_the_text_verbatim_to_the_bot_api():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    result = await _channel(handler).send_text("ATLAS · ideas for Mon 19 Oct\n• MSFT hold")

    assert result == DeliveryResult(ok=True, detail="sent")
    assert len(seen) == 1
    assert seen[0].url == httpx.URL(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage")
    assert json.loads(seen[0].content) == {
        "chat_id": FAKE_CHAT,
        "text": "ATLAS · ideas for Mon 19 Oct\n• MSFT hold",
    }


async def test_send_text_redacts_token_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"all connection attempts failed for {request.url}")

    result = await _channel(handler).send_text("hello")

    assert result.ok is False
    assert FAKE_SECRET not in result.detail and FAKE_TOKEN not in result.detail
    assert "REDACTED" in result.detail


async def test_send_text_redacts_token_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")  # raise_for_status embeds the URL

    result = await _channel(handler).send_text("hello")

    assert result.ok is False
    assert "403" in result.detail
    assert FAKE_SECRET not in result.detail and FAKE_TOKEN not in result.detail
    assert "REDACTED" in result.detail


async def test_send_text_unconfigured_is_a_result_not_a_raise(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("an unconfigured channel must not send anything")

    result = await TelegramChannel(transport=httpx.MockTransport(handler)).send_text("hello")

    assert result == DeliveryResult(ok=False, detail="telegram not configured")


async def test_alert_send_goes_through_the_same_injected_transport():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    result = await _channel(handler).send(
        title="t", body="b", payload={"symbol": "XYZ", "direction": "long"}
    )

    assert result.ok is True and len(seen) == 1
    assert json.loads(seen[0].content)["chat_id"] == FAKE_CHAT


# ------------------------------------------------------------------------- send_digest


async def test_send_digest_sends_through_the_channel_it_is_given():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    result = await send_digest("the digest", channel=_channel(handler))

    assert result.ok is True
    assert json.loads(seen[0].content)["text"] == "the digest"


async def test_send_digest_unconfigured_returns_the_not_configured_result(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    result = await send_digest("the digest")

    assert result == DeliveryResult(ok=False, detail="telegram not configured")


async def test_send_digest_failure_is_a_result_with_no_token_in_it():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom at {request.url}")

    result = await send_digest("the digest", channel=_channel(handler))

    assert result.ok is False
    assert FAKE_SECRET not in result.detail
