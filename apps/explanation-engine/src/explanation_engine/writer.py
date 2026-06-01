"""LLM rationale writer — DeepSeek via the OpenAI-compatible chat API.

Phase 4 changes:
  - Requests strict JSON output (§10.3 contract).
  - Runs safety-repair (§10.4) on every payload before returning.
  - Local LRU cache keyed by symbol + horizon + composite bucket + feature
    hash + news hash (§10.5) so identical re-queries cost zero LLM calls.

Falls back to templated output when no API key is configured or any of:
  JSON parse fails, schema validation fails, or the API call errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from typing import Any

from atlas_shared.logging import get_logger
from atlas_shared.schemas import ExplanationPayload, Signal

from explanation_engine.payload import parse_llm_json, render_markdown, safety_repair
from explanation_engine.prompts import SYSTEM_PROMPT
from explanation_engine.templated import templated_payload

log = get_logger("explanation")

DEFAULT_MODEL = os.environ.get("ATLAS_EXPLAIN_MODEL", "deepseek-chat")
DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MAX_TOKENS = 1024
# 15-min TTL — beyond that, prices have probably moved enough to make the
# cached rationale stale. Configurable via env for tests / heavy use.
DEFAULT_CACHE_TTL_S = float(os.environ.get("ATLAS_EXPLAIN_CACHE_TTL_S", "900"))
DEFAULT_CACHE_SIZE = int(os.environ.get("ATLAS_EXPLAIN_CACHE_SIZE", "256"))

# Indicators worth referencing in the rationale; rest is noise and inflates
# token count without improving the explanation.
_RATIONALE_FEATURES: tuple[str, ...] = (
    "rsi14", "macd", "macd_hist", "bb_pctb",
    "ema9", "ema21", "ema50", "ema200",
    "sma20", "sma50", "sma200",
    "atr14", "adx", "di_plus", "di_minus",
    "obv_slope_z", "rvol_20",
    "stoch_k", "stoch_d",
    "smc_bos", "divergence_bull", "divergence_bear",
    "supertrend_dir", "donchian_upper", "donchian_lower",
    "fib_position", "high_52w_dist", "gap_pct",
    "vol_realized_20",
)


def _trim_features(features: dict[str, Any]) -> dict[str, Any]:
    return {k: features[k] for k in _RATIONALE_FEATURES if k in features}


# ── Cache key (§10.5) ─────────────────────────────────────────────────────────


def _composite_bucket(score: float) -> int:
    """Round to nearest 5 so signals at composite 73 and 74 share a cache slot."""
    return int(round(score / 5.0) * 5)


def _stable_hash(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def make_cache_key(signal: Signal, features: dict[str, Any] | None) -> str:
    """BLUEPRINT §10.5 cache key: symbol + horizon + composite bucket +
    indicator hash + news hash. Two requests with the same key are
    functionally interchangeable from the LLM's perspective."""
    feats = _trim_features(features or {})
    news = {
        "news": signal.sub_scores.news,
        "sent": signal.sub_scores.sent,
    }
    parts = (
        signal.symbol,
        signal.horizon.value,
        signal.direction.value,
        _composite_bucket(signal.composite_score),
        _stable_hash(feats),
        _stable_hash(news),
    )
    return ":".join(str(p) for p in parts)


class _TTLCache:
    """Tiny LRU + TTL hybrid. The stdlib's `functools.lru_cache` has no TTL,
    and `cachetools` would be the third pinned package for a 30-line helper."""

    def __init__(self, max_size: int, ttl_s: float):
        self.max_size = max_size
        self.ttl_s = ttl_s
        self._data: OrderedDict[str, tuple[float, ExplanationPayload]] = OrderedDict()

    def get(self, key: str) -> ExplanationPayload | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        timestamp, payload = entry
        if time.time() - timestamp > self.ttl_s:
            del self._data[key]
            return None
        # Touch — move to most-recent.
        self._data.move_to_end(key)
        return payload

    def set(self, key: str, payload: ExplanationPayload) -> None:
        self._data[key] = (time.time(), payload)
        self._data.move_to_end(key)
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


# ── Writer ────────────────────────────────────────────────────────────────────


def _format_user_message(signal: Signal, features: dict[str, Any] | None) -> str:
    """Compact, schema-validated payload the model can ground in."""
    payload = {
        "signal": signal.model_dump(mode="json"),
        "key_features": _trim_features(features or {}),
    }
    return (
        "Generate the trade report JSON for the following signal. "
        "Cite only the numbers in this payload. If a field is null, render it "
        "as `—`. Respond with raw JSON only.\n\n```json\n"
        + json.dumps(payload, indent=2, default=str)
        + "\n```"
    )


class ExplanationWriter:
    """Stateful wrapper — holds the OpenAI client (pointed at DeepSeek) and
    the local TTL cache so we reuse both across calls."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        cache_size: int = DEFAULT_CACHE_SIZE,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.model = model
        self.base_url = base_url
        self._client = None
        self._cache = _TTLCache(cache_size, cache_ttl_s)
        if self.api_key:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except Exception as e:
                log.warning("explanation.client_init_failed", error=str(e))

    @property
    def available(self) -> bool:
        return self._client is not None

    def clear_cache(self) -> None:
        self._cache.clear()

    def write_payload(
        self,
        signal: Signal,
        features: dict[str, Any] | None = None,
        *,
        use_cache: bool = True,
    ) -> ExplanationPayload:
        """Structured §10.3 payload. Cached by §10.5 key."""
        cache_key = make_cache_key(signal, features) if use_cache else None
        if cache_key is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        if not self.available:
            payload = templated_payload(signal, features)
        else:
            payload = self._call_llm(signal, features) or templated_payload(signal, features)

        payload = safety_repair(payload, signal)
        if cache_key is not None:
            self._cache.set(cache_key, payload)
        return payload

    def write(self, signal: Signal, features: dict[str, Any] | None = None) -> str:
        """Backwards-compatible markdown emitter."""
        payload = self.write_payload(signal, features)
        return render_markdown(payload, signal)

    # ── LLM call ──────────────────────────────────────────────────────────────

    def _call_llm(self, signal: Signal, features: dict[str, Any] | None) -> ExplanationPayload | None:
        assert self._client is not None
        user_msg = _format_user_message(signal, features)
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                # DeepSeek supports OpenAI's `response_format={"type": "json_object"}`
                # — when honoured, eliminates fence-wrap edge cases.
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
        except Exception as e:
            log.warning("explanation.api_error", error=str(e))
            return None

        usage = getattr(response, "usage", None)
        if usage:
            log.info(
                "explanation.usage",
                input=getattr(usage, "prompt_tokens", None),
                cache_hit=getattr(usage, "prompt_cache_hit_tokens", None),
                cache_miss=getattr(usage, "prompt_cache_miss_tokens", None),
                output=getattr(usage, "completion_tokens", None),
            )

        if not response.choices:
            return None
        text = response.choices[0].message.content or ""
        payload = parse_llm_json(text)
        if payload is None:
            log.warning("explanation.parse_failed", sample=text[:200])
            return None
        return payload.model_copy(update={"source": self.model})


# Module-level convenience — reuses a single client + cache.
_writer: ExplanationWriter | None = None


def get_writer() -> ExplanationWriter:
    global _writer
    if _writer is None:
        _writer = ExplanationWriter()
    return _writer


def generate_rationale(signal: Signal, features: dict[str, Any] | None = None) -> str:
    return get_writer().write(signal, features)


def generate_payload(
    signal: Signal,
    features: dict[str, Any] | None = None,
) -> ExplanationPayload:
    return get_writer().write_payload(signal, features)
