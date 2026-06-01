"""Deterministic templated rationale.

Used when no LLM is available (no `DEEPSEEK_API_KEY`) or when the LLM output
fails parsing / safety checks. Emits the same §10.3 structured payload as the
LLM path so consumers don't have to special-case the source.
"""

from __future__ import annotations

from typing import Any

from atlas_shared.schemas import ExplanationPayload, Signal

from explanation_engine.payload import render_markdown, safety_repair


def _top_contributions(signal: Signal, n: int = 3) -> list[tuple[str, float]]:
    subs = signal.sub_scores
    ranked = sorted(
        [
            ("TECHNICAL", subs.tech),
            ("QUANT", subs.quant),
            ("NEWS", subs.news),
            ("SENTIMENT", subs.sent),
            ("MACRO", subs.macro),
            ("OPTIONS", subs.opt),
            ("LIQUIDITY", subs.liq),
            ("RISK", subs.risk),
        ],
        key=lambda kv: abs(kv[1]),
        reverse=True,
    )
    return [(k, v) for k, v in ranked if abs(v) >= 1.0][:n]


def templated_payload(signal: Signal, features: dict[str, Any] | None = None) -> ExplanationPayload:
    """Build the structured payload without calling an LLM.

    The bull/bear cases are deterministic functions of the signal's sub-scores
    and computed levels. Sufficient for journal entries, Telegram alerts, and
    any code-path that hits the rationale layer without an API key configured.
    """
    direction = signal.direction.value
    is_long = direction == "long"

    top = _top_contributions(signal)
    bull = []
    bear = []
    for name, score in top:
        side = "supports" if (is_long and score > 0) or (not is_long and score < 0) else "opposes"
        line = f"{name} ({score:+.0f}) {side} the {direction} direction."
        if side == "supports":
            bull.append(line)
        else:
            bear.append(line)

    # Always at least one explicit invalidation bullet — required by §22 #5.
    for inv in signal.invalidations:
        bear.append(f"Invalidation: {inv}")

    composite = signal.composite_score
    summary = (
        f"{signal.symbol} prints a {signal.conviction.value} {direction} setup at "
        f"composite {composite:+.0f} with {signal.confidence_pct:.0f}% confidence "
        f"under a {signal.regime} regime."
    )

    why_entry = (
        f"Entry at {_fmt(signal.entry_price)} reflects the current close; the "
        f"deterministic engine picked this level rather than chasing — pull-back "
        f"to a confluence area is acceptable if it doesn't violate the stop."
    )
    why_stop = (
        f"Stop at {_fmt(signal.stop_price)} is the tighter of the ATR-based "
        f"distance and the recent Donchian swing low/high with a half-ATR buffer "
        f"(BLUEPRINT §9.3). Closing through this level invalidates the thesis."
    )
    targets = signal.take_profit_levels or []
    target_logic = (
        "Targets ladder at 1R / 2R / 3R from entry; default exit split is "
        "40% at T1, 40% at T2, 20% at T3."
        + (f" T1={_fmt(targets[0])}, T2={_fmt(targets[1])}, T3={_fmt(targets[2])}." if len(targets) >= 3 else "")
    )
    fh = signal.quant_meta.get("feature_health") if signal.quant_meta else None
    confidence_comment = (
        f"Confidence is driven by the calibrated trend-model P(up)="
        f"{signal.quant_meta.get('p_up', 0.5):.2f}"
        if signal.quant_meta else
        f"Confidence is derived from composite magnitude in the absence of a quant model."
    )
    if fh and fh != "ok":
        confidence_comment += f" Note: feature_health={fh} — discount accordingly."

    final_view = (
        f"{signal.symbol} is a {direction} candidate at composite {composite:+.0f}. "
        f"Trade plan above; manage to the stop and invalidations."
    )

    payload = ExplanationPayload(
        summary=summary,
        bull_case=bull,
        bear_case=bear,
        why_entry=why_entry,
        why_stop=why_stop,
        target_logic=target_logic,
        confidence_comment=confidence_comment,
        final_view=final_view,
        source="templated",
        safety_repaired=False,
    )
    return safety_repair(payload, signal)


def templated_rationale(signal: Signal, features: dict[str, Any] | None = None) -> str:
    """Backwards-compatible API — emit markdown directly. Callers that want
    the structured shape should use `templated_payload`."""
    return render_markdown(templated_payload(signal, features), signal)


def _fmt(v: float | None, fmt: str = ".2f") -> str:
    if v is None:
        return "—"
    return format(float(v), fmt)
