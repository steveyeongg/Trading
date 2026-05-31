"""LLM rationale writer — Anthropic Claude with prompt caching.

Falls back to templated output when no API key is configured. The fallback is
deliberate: the signal pipeline must not fail on missing LLM credentials.
"""

from __future__ import annotations

import json
import os
from typing import Any

from atlas_shared.logging import get_logger
from atlas_shared.schemas import Signal

from explanation_engine.prompts import SYSTEM_PROMPT
from explanation_engine.templated import templated_rationale

log = get_logger("explanation")

DEFAULT_MODEL = os.environ.get("ATLAS_EXPLAIN_MODEL", "claude-sonnet-4-6")
DEFAULT_MAX_TOKENS = 1024


def _format_user_message(signal: Signal, features: dict[str, Any] | None) -> str:
    """Compact, schema-validated payload the model can ground in.

    We deliberately serialize as JSON rather than free-form prose: it removes
    one whole class of hallucination (the model interpreting our phrasing) and
    forces it to cite the numbers we supply.
    """
    payload = {
        "signal": signal.model_dump(mode="json"),
        "key_features": _trim_features(features or {}),
    }
    return (
        "Generate the trade report for the following signal. "
        "Cite only the numbers in this payload. If a field is null, render "
        "it as `—`.\n\n```json\n"
        + json.dumps(payload, indent=2, default=str)
        + "\n```"
    )


# Indicators the LLM should reference. Anything else is noise for the
# rationale and just inflates token count.
_RATIONALE_FEATURES = (
    "rsi14", "macd", "macd_hist", "bb_pctb",
    "ema9", "ema21", "ema50", "ema200",
    "atr14", "adx",
    "obv_slope_z", "stoch_k", "stoch_d",
    "smc_bos", "divergence_bull", "divergence_bear",
    "vol_realized_20",
)


def _trim_features(features: dict[str, Any]) -> dict[str, Any]:
    return {k: features[k] for k in _RATIONALE_FEATURES if k in features}


class ExplanationWriter:
    """Stateful wrapper — holds the Anthropic client so we reuse the
    connection and the prompt cache across calls."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        self._client = None
        if self.api_key:
            try:
                from anthropic import Anthropic

                self._client = Anthropic(api_key=self.api_key)
            except Exception as e:
                log.warning("explanation.client_init_failed", error=str(e))

    @property
    def available(self) -> bool:
        return self._client is not None

    def write(self, signal: Signal, features: dict[str, Any] | None = None) -> str:
        if not self.available:
            log.debug("explanation.fallback", reason="no_client")
            return templated_rationale(signal, features)

        user_msg = _format_user_message(signal, features)
        try:
            # The system prompt is sent via the cacheable block. After the
            # first call within the TTL, subsequent input is billed at the
            # cached rate (~10x cheaper).
            response = self._client.messages.create(
                model=self.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as e:
            log.warning("explanation.api_error", error=str(e))
            return templated_rationale(signal, features)

        usage = getattr(response, "usage", None)
        if usage:
            log.info(
                "explanation.usage",
                input=getattr(usage, "input_tokens", None),
                cache_read=getattr(usage, "cache_read_input_tokens", None),
                cache_create=getattr(usage, "cache_creation_input_tokens", None),
                output=getattr(usage, "output_tokens", None),
            )

        if not response.content:
            return templated_rationale(signal, features)
        block = response.content[0]
        return getattr(block, "text", "") or templated_rationale(signal, features)


# Module-level convenience — reuses a single client.
_writer: ExplanationWriter | None = None


def generate_rationale(signal: Signal, features: dict[str, Any] | None = None) -> str:
    global _writer
    if _writer is None:
        _writer = ExplanationWriter()
    return _writer.write(signal, features)
