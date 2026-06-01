"""ATLAS scoring engine."""

from scoring_engine.composite import DEFAULT_WEIGHTS, composite
from scoring_engine.signal import GateResult, generate_signal
from scoring_engine.sub_scores import s_liq, s_news, s_quant, s_risk, s_tech

__all__ = [
    "DEFAULT_WEIGHTS",
    "GateResult",
    "composite",
    "generate_signal",
    "s_liq",
    "s_news",
    "s_quant",
    "s_risk",
    "s_tech",
]
