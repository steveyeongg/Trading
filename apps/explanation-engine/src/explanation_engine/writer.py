"""LLM rationale writer — DeepSeek via the OpenAI-compatible chat API.

DeepSeek (`https://api.deepseek.com`) implements the OpenAI chat/completions
surface byte-for-byte, so we use the `openai` SDK with a custom `base_url`.
That gets us:

  - first-class async/streaming + the same tool-use shape we'd use elsewhere,
  - DeepSeek's *server-side* context caching (transparent — no `cache_control`
    blocks needed; cache hits drop input cost ~10×),
  - drop-in path to other OpenAI-compat backends (OpenRouter, Groq, vLLM) by
    only flipping `base_url` + `api_key`.

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

DEFAULT_MODEL = os.environ.get("ATLAS_EXPLAIN_MODEL", "deepseek-chat")
DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
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
    """Stateful wrapper — holds the OpenAI client (pointed at DeepSeek) so we
    reuse the connection and benefit from DeepSeek's server-side prompt cache
    across calls within the cache TTL."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.model = model
        self.base_url = base_url
        self._client = None
        if self.api_key:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
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
            # DeepSeek caches identical leading content automatically — sending
            # the same long system prompt every call is cheap after the first.
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
        except Exception as e:
            log.warning("explanation.api_error", error=str(e))
            return templated_rationale(signal, features)

        usage = getattr(response, "usage", None)
        if usage:
            # DeepSeek exposes cache hit/miss counts on the usage object
            # (prompt_cache_hit_tokens / prompt_cache_miss_tokens) — useful for
            # observing the cost-savings curve in logs.
            log.info(
                "explanation.usage",
                input=getattr(usage, "prompt_tokens", None),
                cache_hit=getattr(usage, "prompt_cache_hit_tokens", None),
                cache_miss=getattr(usage, "prompt_cache_miss_tokens", None),
                output=getattr(usage, "completion_tokens", None),
            )

        if not response.choices:
            return templated_rationale(signal, features)
        text = response.choices[0].message.content or ""
        return text or templated_rationale(signal, features)


# Module-level convenience — reuses a single client.
_writer: ExplanationWriter | None = None


def generate_rationale(signal: Signal, features: dict[str, Any] | None = None) -> str:
    global _writer
    if _writer is None:
        _writer = ExplanationWriter()
    return _writer.write(signal, features)
