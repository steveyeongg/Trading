"""Bars endpoint — verifies OHLCV→JSON shaping without a live DB.

We stub `latest_bars` (the only DB-touching call) so the route's transform
logic is exercised in isolation. The lightweight-charts client expects
epoch-second `time` plus o/h/l/c/volume.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import signal_service.routes as routes

    async def _fake_latest_bars(symbol: str, resolution: str = "1m", limit: int = 500) -> pd.DataFrame:
        if symbol == "EMPTY":
            return pd.DataFrame()
        ts = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
        return pd.DataFrame(
            [
                {"ts": ts, "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 1000.0},
                {"ts": ts.replace(minute=31), "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 1500.0},
            ]
        )

    monkeypatch.setattr(routes, "latest_bars", _fake_latest_bars)
    # Import the app *after* patching so the route closure sees the stub.
    from signal_service.main import app

    return TestClient(app)


def test_bars_shape(client: TestClient) -> None:
    r = client.get("/v1/symbols/AAPL/bars?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["resolution"] == "1m"
    assert len(body["bars"]) == 2
    first = body["bars"][0]
    assert set(first) == {"time", "open", "high", "low", "close", "volume"}
    assert isinstance(first["time"], int)              # epoch seconds for lightweight-charts
    assert first["time"] == int(datetime(2024, 1, 2, 14, 30, tzinfo=UTC).timestamp())
    assert first["open"] == 100.0 and first["close"] == 100.5


def test_bars_empty_returns_200_with_empty_list(client: TestClient) -> None:
    r = client.get("/v1/symbols/EMPTY/bars")
    assert r.status_code == 200
    assert r.json()["bars"] == []
