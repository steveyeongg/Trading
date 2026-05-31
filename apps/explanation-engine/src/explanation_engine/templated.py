"""Deterministic templated rationale. Used when no LLM is available — keeps
the pipeline working offline and gives us a fixture-able baseline."""

from __future__ import annotations

from typing import Any

from atlas_shared.schemas import Signal


def _fmt(v: float | None, fmt: str = ".2f") -> str:
    if v is None:
        return "—"
    return format(float(v), fmt)


def templated_rationale(signal: Signal, features: dict[str, Any] | None = None) -> str:
    features = features or {}
    direction = signal.direction.value.upper()
    targets = signal.take_profit_levels or []
    t_str = " / ".join(f"${_fmt(t)}" for t in targets) if targets else "—"

    subs = signal.sub_scores
    contributions = sorted(
        [
            ("TECHNICAL", subs.tech),
            ("QUANT", subs.quant),
            ("SENTIMENT", subs.sent),
            ("OPTIONS", subs.opt),
            ("MACRO", subs.macro),
            ("FUNDAMENTAL", subs.fund),
            ("LIQUIDITY", subs.liq),
            ("ON-CHAIN", subs.chain),
        ],
        key=lambda kv: abs(kv[1]),
        reverse=True,
    )
    why_lines = [
        f"{i + 1}. **{name}** ({score:+.0f})"
        for i, (name, score) in enumerate(contributions)
        if abs(score) >= 1.0
    ]

    return (
        f"# SIGNAL: {signal.symbol} — {direction} ({signal.horizon.value})\n"
        f"Composite: {signal.composite_score:+.1f} | Confidence: {signal.confidence_pct:.0f}% | "
        f"Conviction: {signal.conviction.value}\n"
        f"Regime: {signal.regime} · Asset: {signal.asset_class.value}\n\n"
        f"## Trade plan\n"
        f"- Entry: ${_fmt(signal.entry_price)}\n"
        f"- Stop: ${_fmt(signal.stop_price)}\n"
        f"- Targets: {t_str}\n"
        f"- Position size: {_fmt(signal.position_size_pct, '.2f')}% of equity\n"
        f"- Expected R:R: {_fmt(signal.expected_rr, '.2f')}\n\n"
        f"## Why (ordered by contribution)\n"
        + ("\n".join(why_lines) if why_lines else "_(no engine contributed materially)_")
        + "\n\n## Devil's advocate\n"
        "- Signal generated offline with templated rationale; LLM analysis unavailable.\n"
        "- Verify against current macro calendar (FOMC, CPI, earnings) before sizing up.\n\n"
        "## Disclaimer\n"
        "Informational only. Not investment advice."
    )
