"""Smoke test that the composite score plumbing works end-to-end.

Weights are anchored to BLUEPRINT §8.2:
  tech 0.30, quant 0.25, news 0.10, sent 0.10, macro 0.10, opt 0.05, liq 0.05, risk 0.05.
"""

from __future__ import annotations

from atlas_shared.schemas import SubScores
from scoring_engine.composite import composite


def test_zero_subs_yields_zero() -> None:
    assert composite(SubScores()) == 0.0


def test_clamps_to_upper_bound() -> None:
    subs = SubScores(
        tech=100, quant=100, news=100, sent=100, macro=100, opt=100, liq=100, risk=100
    )
    assert composite(subs) == 100.0


def test_clamps_to_lower_bound() -> None:
    subs = SubScores(
        tech=-100, quant=-100, news=-100, sent=-100, macro=-100, opt=-100, liq=-100, risk=-100
    )
    assert composite(subs) == -100.0


def test_weighted_blend_matches_blueprint_defaults() -> None:
    # tech=+50 alone @ weight 0.30 → +15.
    assert composite(SubScores(tech=50)) == 15.0
    # quant=+80 alone @ weight 0.25 → +20.
    assert composite(SubScores(quant=80)) == 20.0
    # news=+100 alone @ weight 0.10 → +10.
    assert composite(SubScores(news=100)) == 10.0
