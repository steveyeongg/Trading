"""Walk-forward CV with purge + embargo around overlapping labels."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Split:
    train: np.ndarray
    test: np.ndarray


def walk_forward_purged(
    n: int,
    n_splits: int = 5,
    test_size: int = 200,
    embargo: int = 10,
    label_horizon: int = 20,
) -> Iterator[Split]:
    """Yield expanding-window splits with a purge gap of `label_horizon` bars
    between train end and test start, plus an `embargo` after the test window.

    Required because triple-barrier labels at training-tail can peek into
    test-region prices via their forward window. AFML ch. 7.
    """
    if n_splits < 1:
        raise ValueError("n_splits must be >= 1")
    if test_size * n_splits >= n:
        raise ValueError(f"need n > test_size*n_splits; got n={n}")

    end = n
    for s in range(n_splits):
        test_end = end - s * (test_size + embargo)
        test_start = test_end - test_size
        train_end = test_start - label_horizon  # purge
        if train_end <= 0 or test_start <= 0:
            break
        train = np.arange(0, train_end)
        test = np.arange(test_start, test_end)
        yield Split(train=train, test=test)
