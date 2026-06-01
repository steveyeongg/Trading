"""ATLAS explanation engine."""

from explanation_engine.payload import (
    DISCLAIMER,
    parse_llm_json,
    render_markdown,
    safety_repair,
)
from explanation_engine.templated import templated_payload, templated_rationale
from explanation_engine.writer import (
    ExplanationWriter,
    generate_payload,
    generate_rationale,
    get_writer,
    make_cache_key,
)

__all__ = [
    "DISCLAIMER",
    "ExplanationWriter",
    "generate_payload",
    "generate_rationale",
    "get_writer",
    "make_cache_key",
    "parse_llm_json",
    "render_markdown",
    "safety_repair",
    "templated_payload",
    "templated_rationale",
]
