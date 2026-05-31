"""Performance metrics. Cost-aware, deflated where appropriate.

Convention: `returns` is a pd.Series/np.ndarray of *period* (e.g. daily or
per-bar) simple returns. `equity` is the cumulative equity series.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats


def _to_array(x) -> np.ndarray:
    if isinstance(x, pd.Series):
        return x.to_numpy(dtype=float)
    return np.asarray(x, dtype=float)


def sharpe(returns, periods_per_year: int = 252, rf: float = 0.0) -> float:
    r = _to_array(returns)
    if r.size < 2:
        return float("nan")
    excess = r - rf / periods_per_year
    sd = float(np.std(excess, ddof=1))
    if sd == 0:
        return float("nan")
    return float(np.mean(excess) / sd * math.sqrt(periods_per_year))


def sortino(returns, periods_per_year: int = 252, rf: float = 0.0) -> float:
    r = _to_array(returns)
    excess = r - rf / periods_per_year
    downside = excess[excess < 0]
    if downside.size < 2:
        return float("nan")
    dd = float(np.std(downside, ddof=1))
    if dd == 0:
        return float("nan")
    return float(np.mean(excess) / dd * math.sqrt(periods_per_year))


def max_drawdown(equity) -> float:
    e = _to_array(equity)
    if e.size == 0:
        return 0.0
    running_max = np.maximum.accumulate(e)
    dd = (running_max - e) / running_max
    return float(np.nanmax(dd))


def cagr(equity_curve: list[tuple], periods_per_year: int = 252) -> float:
    if len(equity_curve) < 2:
        return float("nan")
    e0 = equity_curve[0][1]
    e1 = equity_curve[-1][1]
    n_periods = len(equity_curve)
    years = n_periods / periods_per_year
    if years <= 0 or e0 <= 0:
        return float("nan")
    return float((e1 / e0) ** (1 / years) - 1)


def calmar(equity, periods_per_year: int = 252) -> float:
    e = _to_array(equity)
    if e.size < 2:
        return float("nan")
    rets = np.diff(e) / e[:-1]
    cagr_v = (e[-1] / e[0]) ** (periods_per_year / len(rets)) - 1
    mdd = max_drawdown(e)
    if mdd == 0:
        return float("nan")
    return float(cagr_v / mdd)


def profit_factor(returns) -> float:
    r = _to_array(returns)
    gains = r[r > 0].sum()
    losses = -r[r < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return float(gains / losses)


def expectancy(trade_returns) -> float:
    r = _to_array(trade_returns)
    if r.size == 0:
        return float("nan")
    return float(np.mean(r))


def hit_rate(trade_returns) -> float:
    r = _to_array(trade_returns)
    if r.size == 0:
        return float("nan")
    return float(np.mean(r > 0))


def deflated_sharpe(
    observed_sharpe: float,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Bailey & López de Prado (2014). Probability that the *true* Sharpe is
    > 0 given multiple-testing over `n_trials`. Returns a probability in [0, 1].
    """
    if n_obs < 10 or n_trials < 1 or not math.isfinite(observed_sharpe):
        return float("nan")
    # Expected max Sharpe under H0 (no skill) across n_trials.
    emc = 0.5772156649  # Euler-Mascheroni
    e_max = (1 - emc) * stats.norm.ppf(1 - 1 / n_trials) + emc * stats.norm.ppf(1 - 1 / (n_trials * math.e))
    # Variance term — adjusts for non-normal returns.
    excess_kurt = kurtosis - 3.0
    var_term = (1 - skew * observed_sharpe + (excess_kurt / 4) * observed_sharpe**2) / (n_obs - 1)
    if var_term <= 0:
        return float("nan")
    z = (observed_sharpe - e_max) / math.sqrt(var_term)
    return float(stats.norm.cdf(z))


def bootstrap_sharpe(
    returns,
    n_boot: int = 1000,
    block_size: int = 20,
    periods_per_year: int = 252,
    seed: int = 0,
) -> tuple[float, float]:
    """Block bootstrap CI on Sharpe (5%, 95%) — preserves serial correlation."""
    r = _to_array(returns)
    if r.size < block_size * 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    n = r.size
    n_blocks = n // block_size
    estimates = np.empty(n_boot)
    for i in range(n_boot):
        # Resample blocks with replacement.
        idx_starts = rng.integers(0, n - block_size + 1, size=n_blocks)
        sample = np.concatenate([r[s : s + block_size] for s in idx_starts])
        sd = float(np.std(sample, ddof=1))
        estimates[i] = float("nan") if sd == 0 else float(np.mean(sample) / sd * math.sqrt(periods_per_year))
    lo, hi = np.nanpercentile(estimates, [5, 95])
    return float(lo), float(hi)


def summarise(
    equity_curve: list[tuple],
    trades: list,  # list[Trade], unimported here to avoid cycle
    periods_per_year: int = 252,
) -> dict[str, float]:
    if not equity_curve:
        return {}
    equity = np.array([e for _, e in equity_curve], dtype=float)
    bar_returns = np.diff(equity) / equity[:-1]
    trade_returns = np.array(
        [t.realized_return for t in trades if t.realized_return is not None],
        dtype=float,
    )
    sh = sharpe(bar_returns, periods_per_year=periods_per_year)
    boot_lo, boot_hi = bootstrap_sharpe(bar_returns, periods_per_year=periods_per_year)
    return {
        "final_equity": float(equity[-1]),
        "total_return": float(equity[-1] / equity[0] - 1),
        "cagr": cagr(equity_curve, periods_per_year=periods_per_year),
        "max_drawdown": max_drawdown(equity),
        "sharpe": sh,
        "sharpe_ci_lo": boot_lo,
        "sharpe_ci_hi": boot_hi,
        "sortino": sortino(bar_returns, periods_per_year=periods_per_year),
        "calmar": calmar(equity, periods_per_year=periods_per_year),
        "profit_factor": profit_factor(trade_returns),
        "expectancy_per_trade": expectancy(trade_returns),
        "hit_rate": hit_rate(trade_returns),
        "n_trades": float(len(trades)),
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])) if trades else float("nan"),
    }
