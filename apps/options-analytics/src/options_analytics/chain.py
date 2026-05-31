"""Option-chain data classes + a synthetic generator for offline dev/tests.

A real vendor adapter (Polygon/Tradier/Unusual Whales) would produce the same
`OptionChain` shape; the synthetic generator lets the whole s_opt path run
without a paid options feed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from options_analytics.greeks import bs_price, implied_vol


@dataclass(frozen=True)
class OptionQuote:
    strike: float
    right: str          # "call" | "put"
    expiry_days: int
    open_interest: float
    volume: float
    iv: float           # implied vol (decimal)
    last: float


@dataclass
class OptionChain:
    symbol: str
    spot: float
    quotes: list[OptionQuote] = field(default_factory=list)
    # 30d ATM IV history (decimal) for IV-rank; newest last.
    iv30_history: list[float] = field(default_factory=list)


def synthetic_chain(
    symbol: str,
    spot: float,
    *,
    seed: int = 0,
    base_iv: float = 0.28,
    skew: float = 0.0,          # positive → put skew (fear); negative → call skew
    expiry_days: int = 30,
    n_strikes: int = 11,
    pc_oi_ratio: float = 1.0,   # put OI / call OI; >1 = more puts
) -> OptionChain:
    """Deterministic chain around `spot`. Strikes span ±25%. IV follows a
    smile plus `skew`. OI/volume are plausible and seed-stable."""
    rng = np.random.default_rng(abs(hash((symbol, seed))) & 0xFFFF)
    strikes = np.linspace(spot * 0.75, spot * 1.25, n_strikes)
    t = expiry_days / 365.0
    quotes: list[OptionQuote] = []
    for k in strikes:
        moneyness = (k - spot) / spot
        smile = base_iv + 0.5 * moneyness**2          # vol smile
        for right in ("call", "put"):
            # Put skew lifts downside-put IV; call skew the inverse.
            tilt = skew * (-moneyness if right == "put" else moneyness)
            iv = max(0.05, smile + tilt)
            price = bs_price(spot, float(k), t, 0.04, iv, right)
            oi_base = float(rng.integers(200, 5000))
            oi = oi_base * (pc_oi_ratio if right == "put" else 1.0)
            vol = oi * float(rng.uniform(0.1, 0.6))
            quotes.append(
                OptionQuote(
                    strike=float(k), right=right, expiry_days=expiry_days,
                    open_interest=oi, volume=vol, iv=float(iv), last=float(price),
                )
            )
    # IV-rank history: a year of ATM IV around base_iv.
    hist = list(base_iv + 0.06 * np.sin(np.linspace(0, 6.28, 252)) + rng.normal(0, 0.01, 252))
    hist.append(base_iv)
    return OptionChain(symbol=symbol, spot=spot, quotes=quotes, iv30_history=[float(x) for x in hist])


def iv_from_quote(q: OptionQuote, spot: float, r: float = 0.04) -> float:
    """Recover IV from a quote's last price (when a feed omits IV)."""
    return implied_vol(q.last, spot, q.strike, q.expiry_days / 365.0, r, q.right)
