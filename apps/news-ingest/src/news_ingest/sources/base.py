"""Base classes for news sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RawItem:
    """Raw, source-agnostic news payload before scoring & persistence."""

    source: str
    external_id: str | None
    title: str
    url: str | None
    published_at: datetime
    summary: str | None = None
    body: str | None = None
    tickers: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)


class NewsSource(Protocol):
    """Each source is async; return a list of RawItems."""

    name: str

    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        ...
