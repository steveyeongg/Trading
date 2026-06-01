"""Derived signal-event flags — BLUEPRINT §12.2.

The alert engine fires on metrics already in the signal payload (e.g.
`composite_score`). §12.2 expands that to seven triggers including
*derived* events that require state across calls — "signal is new",
"price reached T1", "macro regime changed".

This module computes those derived booleans by diffing the current signal
payload against the previous one for the same symbol. Engine holds a
small in-memory state map keyed by symbol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SignalEvents:
    """All seven §12.2 triggers as booleans. Each is independently rule-matchable."""

    signal_new: bool
    signal_upgraded: bool          # composite |score| moved up by >= 5 in same direction
    composite_threshold_crossed: bool
    price_reached_entry: bool
    price_reached_t1: bool
    price_reached_t2: bool
    price_reached_t3: bool
    price_hit_stop: bool
    risk_veto_changed: bool
    macro_regime_changed: bool

    def as_dict(self) -> dict[str, bool]:
        return {
            "signal_new": self.signal_new,
            "signal_upgraded": self.signal_upgraded,
            "composite_threshold_crossed": self.composite_threshold_crossed,
            "price_reached_entry": self.price_reached_entry,
            "price_reached_t1": self.price_reached_t1,
            "price_reached_t2": self.price_reached_t2,
            "price_reached_t3": self.price_reached_t3,
            "price_hit_stop": self.price_hit_stop,
            "risk_veto_changed": self.risk_veto_changed,
            "macro_regime_changed": self.macro_regime_changed,
        }


def _reached(direction: str, level: float | None, last_price: float) -> bool:
    """Did `last_price` cross `level` in the direction-implied side?

    Long entry/T1/T2/T3: price reaching at-or-above the level → True.
    Short entry/T1/T2/T3: price reaching at-or-below the level → True.
    """
    if level is None:
        return False
    if direction == "long":
        return last_price >= level
    return last_price <= level


def _hit_stop(direction: str, stop: float | None, last_price: float) -> bool:
    if stop is None:
        return False
    if direction == "long":
        return last_price <= stop
    return last_price >= stop


def derive_events(
    *,
    current: dict[str, Any] | None,
    last_price: float | None,
    prior_signal: dict[str, Any] | None,
    prior_regime: str | None,
    current_regime: str | None,
    composite_threshold: float = 60.0,
) -> SignalEvents:
    """Compute the §12.2 trigger set.

    `current` is the just-published Signal dict (or None if the gates failed).
    `prior_signal` is what was last published for this symbol; pass None on
    first sighting. `last_price` is the most recent bar close used to detect
    level breaches.
    """
    no_current = current is None

    # Signal lifecycle — new / upgraded.
    if no_current:
        signal_new = False
        signal_upgraded = False
    else:
        if prior_signal is None:
            signal_new = True
            signal_upgraded = False
        else:
            prior_dir = prior_signal.get("direction")
            cur_dir = current.get("direction")
            signal_new = prior_dir != cur_dir
            prior_score = abs(prior_signal.get("composite_score", 0.0) or 0.0)
            cur_score = abs(current.get("composite_score", 0.0) or 0.0)
            signal_upgraded = (
                not signal_new and cur_dir == prior_dir and (cur_score - prior_score) >= 5.0
            )

    # Composite-threshold crossing — used by the existing scalar rules but
    # surfaced as its own event for explicit rule matching.
    composite_crossed = False
    if not no_current:
        prior_score = abs((prior_signal or {}).get("composite_score", 0.0) or 0.0)
        cur_score = abs(current.get("composite_score", 0.0) or 0.0)
        composite_crossed = prior_score < composite_threshold <= cur_score

    # Price-level events — use the *current* trade plan if present, else fall
    # back to the prior signal's levels so we can still alert "price hit T1"
    # after the gates flip the signal off.
    plan_src = current or prior_signal or {}
    direction = plan_src.get("direction") or "long"
    tps = plan_src.get("take_profit_levels") or []
    entry = plan_src.get("entry_price")
    stop = plan_src.get("stop_price")
    t1 = tps[0] if len(tps) >= 1 else None
    t2 = tps[1] if len(tps) >= 2 else None
    t3 = tps[2] if len(tps) >= 3 else None

    if last_price is None:
        reached_entry = reached_t1 = reached_t2 = reached_t3 = hit_stop = False
    else:
        reached_entry = _reached(direction, entry, last_price)
        reached_t1 = _reached(direction, t1, last_price)
        reached_t2 = _reached(direction, t2, last_price)
        reached_t3 = _reached(direction, t3, last_price)
        hit_stop = _hit_stop(direction, stop, last_price)

    # Veto state change — published vs vetoed flip.
    prior_published = prior_signal is not None
    current_published = current is not None
    risk_veto_changed = prior_published != current_published

    # Regime change.
    regime_changed = bool(
        prior_regime is not None and current_regime is not None and prior_regime != current_regime
    )

    return SignalEvents(
        signal_new=signal_new,
        signal_upgraded=signal_upgraded,
        composite_threshold_crossed=composite_crossed,
        price_reached_entry=reached_entry,
        price_reached_t1=reached_t1,
        price_reached_t2=reached_t2,
        price_reached_t3=reached_t3,
        price_hit_stop=hit_stop,
        risk_veto_changed=risk_veto_changed,
        macro_regime_changed=regime_changed,
    )
