"""File-based source — JSONL replay.

Useful for offline dev (no network), deterministic tests, and replaying a
historical news archive when running backtests with sentiment in the loop.

Expected JSONL line shape (extra keys ignored):
    {"title": "...", "url": "...", "published_at": "2024-05-01T12:30:00Z",
     "summary": "...", "source": "manual", "tickers": ["AAPL"]}
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from atlas_shared.logging import get_logger

from news_ingest.sources.base import RawItem

log = get_logger("news.file")


class FileSource:
    name = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    async def fetch(self, since: datetime | None = None) -> list[RawItem]:
        if not self.path.exists():
            log.warning("file.missing", path=str(self.path))
            return []
        items: list[RawItem] = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _iso(d.get("published_at"))
            if since and ts < since:
                continue
            items.append(RawItem(
                source=d.get("source", "file"),
                external_id=d.get("external_id") or d.get("url") or d.get("title"),
                title=d.get("title", ""),
                url=d.get("url"),
                summary=d.get("summary"),
                body=d.get("body"),
                published_at=ts,
                tickers=tuple(d.get("tickers", [])),
                metadata=d.get("metadata", {}),
            ))
        log.info("file.fetch.done", path=str(self.path), n=len(items))
        return items


def _iso(s: str | None) -> datetime:
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
