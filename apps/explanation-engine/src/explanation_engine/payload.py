"""Structured explanation payload — BLUEPRINT §10.3 + §10.4.

The DeepSeek-emitted JSON, the templated fallback, and the markdown renderer
all share this shape. The signal pipeline stores both the structured payload
and a rendered markdown blob (`Signal.rationale_md`) — consumers pick whichever
they need.
"""

from __future__ import annotations

import json
import re
from typing import Any

from atlas_shared.schemas import ExplanationPayload, Signal

# BLUEPRINT §10.4 safety rules — these strings must never appear in the
# user-facing rationale (case-insensitive). Hits are stripped or rewritten,
# and `safety_repaired` is flipped so callers know the LLM tried to violate.
_FORBIDDEN_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bguaranteed\b", re.IGNORECASE), "likely"),
    (re.compile(r"\bguarantees?\b", re.IGNORECASE), "may"),
    (re.compile(r"\b(?:risk[\s-]?free|no\s+risk)\b", re.IGNORECASE), "lower-risk"),
    (re.compile(r"\bsure\s+thing\b", re.IGNORECASE), "high-conviction setup"),
    (re.compile(r"\bcan'?t\s+lose\b", re.IGNORECASE), "favourable risk-reward"),
)

DISCLAIMER = "Informational only. Not financial advice. Trading involves risk."


def _strip_forbidden(text: str) -> tuple[str, bool]:
    """Apply the §10.4 phrase substitutions. Returns (new_text, was_modified)."""
    modified = False
    out = text
    for pattern, replacement in _FORBIDDEN_PHRASES:
        new = pattern.sub(replacement, out)
        if new != out:
            modified = True
            out = new
    return out, modified


def _ensure_invalidation(items: list[str], signal: Signal) -> tuple[list[str], bool]:
    """BLUEPRINT §22 non-negotiable — every user-facing rationale must include
    at least one invalidation. If the LLM omitted them, fold the deterministic
    invalidations from the Signal into the bear case."""
    if items:
        return items, False
    fallbacks: list[str] = []
    for inv in signal.invalidations:
        fallbacks.append(f"Invalidation: {inv}")
    return fallbacks or ["Invalidation: thesis fails if price violates the stop."], True


def safety_repair(payload: ExplanationPayload, signal: Signal) -> ExplanationPayload:
    """Run the §10.4 safety rules against a populated payload. Returns a new
    payload with forbidden phrases stripped and invalidation guaranteed."""
    modified = payload.safety_repaired
    new_fields: dict[str, Any] = {}
    for field_name in ("summary", "why_entry", "why_stop", "target_logic",
                       "confidence_comment", "final_view"):
        before = getattr(payload, field_name) or ""
        after, was = _strip_forbidden(before)
        modified = modified or was
        new_fields[field_name] = after

    bull: list[str] = []
    bear: list[str] = []
    for item in payload.bull_case:
        repaired, was = _strip_forbidden(item)
        modified = modified or was
        bull.append(repaired)
    for item in payload.bear_case:
        repaired, was = _strip_forbidden(item)
        modified = modified or was
        bear.append(repaired)
    bear, added_inv = _ensure_invalidation(bear, signal)
    modified = modified or added_inv

    return payload.model_copy(update={
        **new_fields,
        "bull_case": bull,
        "bear_case": bear,
        "safety_repaired": modified,
    })


# ── JSON ↔ payload conversion ─────────────────────────────────────────────────


def parse_llm_json(raw: str) -> ExplanationPayload | None:
    """Parse the LLM's JSON response. Tolerates the model wrapping the JSON in
    a ```json fenced block. Returns None on any parse error so the caller can
    fall back to templated."""
    text = raw.strip()
    if text.startswith("```"):
        # Strip fenced code block — first fence + optional language tag, last fence.
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ExplanationPayload(**data)
    except Exception:
        return None


# ── Markdown render ───────────────────────────────────────────────────────────


def render_markdown(payload: ExplanationPayload, signal: Signal) -> str:
    """Render the structured payload to markdown for `Signal.rationale_md`.

    Layout matches the existing dashboard expectations (the title line is
    asserted in `test_explanation.py`) but the body is restructured around the
    §10.3 sections.
    """
    direction = signal.direction.value.upper()
    targets = signal.take_profit_levels or []
    t_str = " / ".join(f"${_fmt(t)}" for t in targets) if targets else "—"

    def _bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "_(none listed)_"

    return (
        f"# SIGNAL: {signal.symbol} — {direction} ({signal.horizon.value})\n"
        f"Composite: {signal.composite_score:+.1f} | "
        f"Confidence: {signal.confidence_pct:.0f}% | "
        f"Conviction: {signal.conviction.value}\n"
        f"Regime: {signal.regime} · Asset: {signal.asset_class.value}\n\n"
        f"## Trade plan\n"
        f"- Entry: ${_fmt(signal.entry_price)}\n"
        f"- Stop: ${_fmt(signal.stop_price)}\n"
        f"- Targets: {t_str}\n"
        f"- Position size: {_fmt(signal.position_size_pct, '.2f')}% of equity\n"
        f"- Expected R:R: {_fmt(signal.expected_rr, '.2f')}\n\n"
        f"## Summary\n{payload.summary or '—'}\n\n"
        f"## Bull case\n{_bullets(payload.bull_case)}\n\n"
        f"## Bear case\n{_bullets(payload.bear_case)}\n\n"
        f"## Why entry\n{payload.why_entry or '—'}\n\n"
        f"## Why stop\n{payload.why_stop or '—'}\n\n"
        f"## Target logic\n{payload.target_logic or '—'}\n\n"
        f"## Confidence\n{payload.confidence_comment or '—'}\n\n"
        f"## Final view\n{payload.final_view or '—'}\n\n"
        f"## Disclaimer\n{DISCLAIMER}\n"
        f"<!-- explanation_source={payload.source} safety_repaired={payload.safety_repaired} -->"
    )


def _fmt(v: float | None, fmt: str = ".2f") -> str:
    if v is None:
        return "—"
    return format(float(v), fmt)
