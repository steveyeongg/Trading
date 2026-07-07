"""Alert rule model + predicate evaluation.

A rule fires when a signal's chosen `metric` satisfies `op threshold`, with
optional symbol/direction filters. Pure functions — no I/O — so evaluation is
trivially testable.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}

# Sub-score metrics resolve against signal.sub_scores; everything else is a
# top-level signal field. Must match the SubScores schema keys — pre-Phase-1
# this had stale `fund`/`chain` slots that never matched, silently swallowing
# rules keyed on those metrics.
_SUB_METRICS = {"tech", "quant", "news", "sent", "macro", "opt", "liq", "risk"}


@dataclass(frozen=True)
class AlertRule:
    id: str
    name: str
    metric: str
    op: str
    threshold: float
    symbol: str | None = None
    direction: str | None = None
    channels: tuple[str, ...] = ("log",)
    cooldown_s: int = 1800
    enabled: bool = True


def metric_value(signal: dict[str, Any], metric: str) -> float | None:
    """Extract the metric value from a signal dict (as produced by
    Signal.model_dump). Returns None when absent."""
    if metric in _SUB_METRICS:
        subs = signal.get("sub_scores") or {}
        v = subs.get(metric)
    elif metric == "composite":
        v = signal.get("composite_score")
    elif metric == "confidence":
        v = signal.get("confidence_pct")
    else:
        v = signal.get(metric)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def matches(rule: AlertRule, signal: dict[str, Any]) -> bool:
    """True if the rule fires for this signal (filters + predicate)."""
    if not rule.enabled:
        return False
    if rule.symbol and signal.get("symbol", "").upper() != rule.symbol.upper():
        return False
    if rule.direction and signal.get("direction") != rule.direction:
        return False
    op = _OPS.get(rule.op)
    if op is None:
        return False
    val = metric_value(signal, rule.metric)
    if val is None:
        return False
    # For directional comfort: a composite rule with op '>=' on a SHORT signal
    # should compare against the *signed* composite as-is — the user can add a
    # direction filter if they want one-sided behaviour.
    return op(val, rule.threshold)


@dataclass
class FiredAlert:
    rule: AlertRule
    symbol: str
    signal: dict[str, Any]
    triggered_value: float
    channels: tuple[str, ...] = field(default_factory=tuple)
