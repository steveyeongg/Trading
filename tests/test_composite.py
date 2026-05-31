"""Smoke test that the composite score plumbing works end-to-end."""

from __future__ import annotations

from atlas_shared.schemas import SubScores
from scoring_engine.composite import composite


def test_zero_subs_yields_zero() -> None:
    assert composite(SubScores()) == 0.0


def test_clamps_to_upper_bound() -> None:
    subs = SubScores(tech=100, quant=100, fund=100, macro=100, sent=100, opt=100, liq=100, chain=100)
    assert composite(subs) == 100.0


def test_clamps_to_lower_bound() -> None:
    subs = SubScores(tech=-100, quant=-100, fund=-100, macro=-100, sent=-100, opt=-100, liq=-100, chain=-100)
    assert composite(subs) == -100.0


def test_weighted_blend_matches_blueprint_defaults() -> None:
    # tech=+50 alone @ weight 0.20 → +10.
    assert composite(SubScores(tech=50)) == 10.0
    # quant=+80 alone @ weight 0.25 → +20.
    assert composite(SubScores(quant=80)) == 20.0
