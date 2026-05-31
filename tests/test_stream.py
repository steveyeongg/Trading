"""WebSocket stream: connect, immediate regime push, subscribe ack, wildcard
match, ping/pong. No DB needed — regime falls back to 'unknown'."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_stream_immediate_regime_and_ack() -> None:
    from signal_service.main import app

    with TestClient(app) as client, client.websocket_connect("/v1/stream") as ws:
        # On connect the server pushes a regime snapshot straight to the socket.
        first = ws.receive_json()
        assert first["subject"] == "regime.global"
        assert "regime" in first["data"]

        # Subscribe → ack.
        ws.send_json({"subscribe": ["regime.global", "signals.AAPL"]})
        ack = ws.receive_json()
        assert ack["subject"] == "_ack"
        assert set(ack["data"]["subscribed"]) == {"regime.global", "signals.AAPL"}

        # Ping → pong.
        ws.send_json({"ping": 1})
        pong = ws.receive_json()
        assert pong["subject"] == "_pong"


def test_manager_wildcard_match() -> None:
    from signal_service.stream import ConnectionManager

    m = ConnectionManager()
    assert m._matches({"signals.*"}, "signals.AAPL")
    assert m._matches({"regime.global"}, "regime.global")
    assert not m._matches({"signals.AAPL"}, "signals.MSFT")
    assert not m._matches(set(), "regime.global")


def test_manager_subscribed_symbols_excludes_wildcards() -> None:
    from signal_service.stream import ConnectionManager

    m = ConnectionManager()

    class _FakeWS:
        pass

    ws = _FakeWS()
    m._subs[ws] = {"signals.AAPL", "signals.msft", "signals.*", "regime.global"}
    assert m.subscribed_symbols() == {"AAPL", "MSFT"}
