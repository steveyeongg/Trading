"""Chain-level analytics: put/call ratio, IV rank, max pain, dealer GEX.

These feed the options feature vector that `s_opt` scores.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from options_analytics.chain import OptionChain
from options_analytics.greeks import bs_gamma


@dataclass
class OptionsFeatures:
    put_call_oi: float          # put OI / call OI (>1 = bearish positioning)
    put_call_volume: float
    iv30: float
    iv_rank: float              # 0..1 percentile of iv30 in its 1y history
    max_pain: float             # strike minimizing total option-holder payout
    max_pain_distance: float    # (spot - max_pain) / spot
    gex: float                  # net dealer gamma exposure ($/1% move, signed)
    gex_flip: float             # strike where cumulative GEX flips sign


def _sum_oi(chain: OptionChain, right: str) -> float:
    return sum(q.open_interest for q in chain.quotes if q.right == right)


def _sum_vol(chain: OptionChain, right: str) -> float:
    return sum(q.volume for q in chain.quotes if q.right == right)


def put_call_oi(chain: OptionChain) -> float:
    calls = _sum_oi(chain, "call")
    puts = _sum_oi(chain, "put")
    return puts / calls if calls > 0 else float("inf")


def put_call_volume(chain: OptionChain) -> float:
    calls = _sum_vol(chain, "call")
    puts = _sum_vol(chain, "put")
    return puts / calls if calls > 0 else float("inf")


def iv_rank(chain: OptionChain) -> tuple[float, float]:
    """Return (current_iv30, iv_rank in 0..1)."""
    hist = list(chain.iv30_history)
    if not hist:
        return float("nan"), float("nan")
    cur = hist[-1]
    lo, hi = min(hist), max(hist)
    rank = 0.0 if hi == lo else (cur - lo) / (hi - lo)
    return float(cur), float(rank)


def max_pain(chain: OptionChain) -> float:
    """Strike that minimizes total intrinsic payout to option holders at expiry
    — i.e. where the most open interest expires worthless ('pain' for buyers)."""
    strikes = sorted({q.strike for q in chain.quotes})
    if not strikes:
        return chain.spot
    best_k, best_pain = strikes[0], float("inf")
    for k in strikes:
        pain = 0.0
        for q in chain.quotes:
            if q.right == "call":
                pain += max(0.0, k - q.strike) * q.open_interest
            else:
                pain += max(0.0, q.strike - k) * q.open_interest
        if pain < best_pain:
            best_pain, best_k = pain, k
    return float(best_k)


def gamma_exposure(chain: OptionChain, r: float = 0.04, contract_mult: float = 100.0) -> tuple[float, float]:
    """Net dealer GEX and the gamma-flip strike.

    Convention: dealers are long calls / short puts from customer flow → call
    gamma adds positive GEX, put gamma negative. GEX per contract is
    gamma * OI * mult * spot^2 * 0.01 ($ per 1% move). Positive GEX → dealers
    suppress volatility (mean-reverting); negative → amplify (trending).
    """
    spot = chain.spot
    per_strike: dict[float, float] = {}
    total = 0.0
    for q in chain.quotes:
        t = max(q.expiry_days / 365.0, 1e-6)
        gamma = bs_gamma(spot, q.strike, t, r, max(q.iv, 1e-3))
        notional = gamma * q.open_interest * contract_mult * (spot**2) * 0.01
        signed = notional if q.right == "call" else -notional
        total += signed
        per_strike[q.strike] = per_strike.get(q.strike, 0.0) + signed

    # Gamma-flip: walk strikes ascending, find where cumulative GEX crosses 0.
    flip = spot
    cum = 0.0
    prev_k = None
    for k in sorted(per_strike):
        new_cum = cum + per_strike[k]
        if prev_k is not None and (cum < 0 <= new_cum or cum > 0 >= new_cum):
            flip = k
            break
        cum, prev_k = new_cum, k
    return float(total), float(flip)


def compute_features(chain: OptionChain) -> OptionsFeatures:
    iv30, ivr = iv_rank(chain)
    mp = max_pain(chain)
    gex, flip = gamma_exposure(chain)
    return OptionsFeatures(
        put_call_oi=put_call_oi(chain),
        put_call_volume=put_call_volume(chain),
        iv30=iv30,
        iv_rank=ivr,
        max_pain=mp,
        max_pain_distance=(chain.spot - mp) / chain.spot if chain.spot else 0.0,
        gex=gex,
        gex_flip=flip,
    )


def features_dict(chain: OptionChain) -> dict[str, float]:
    return asdict(compute_features(chain))


# Keep the float-NaN guard handy for callers.
def _finite(x: float, default: float = 0.0) -> float:
    return x if np.isfinite(x) else default
