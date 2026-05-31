"""Channel interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    detail: str = ""


class Channel(Protocol):
    name: str

    @property
    def available(self) -> bool:
        """False when required credentials/config are missing — the engine
        skips unavailable channels instead of erroring."""
        ...

    async def send(self, *, title: str, body: str, payload: dict[str, Any]) -> DeliveryResult:
        ...


def format_alert(payload: dict[str, Any]) -> tuple[str, str]:
    """Shared human-readable rendering used by text-y channels."""
    sym = payload.get("symbol", "?")
    rule = payload.get("rule_name", "alert")
    metric = payload.get("metric", "")
    value = payload.get("value")
    direction = (payload.get("direction") or "").upper()
    composite = payload.get("composite")
    conf = payload.get("confidence")
    title = f"ATLAS · {sym} · {rule}"
    body = (
        f"{sym} {direction} — {rule}\n"
        f"{metric} = {value}\n"
        f"composite {composite} · confidence {conf}%"
    )
    return title, body
