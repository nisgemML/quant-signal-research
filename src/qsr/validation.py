"""Chronological splits with purging.

Layout of the sample (time runs left to right):

    |<------------------ development ------------------>|<---- holdout ---->|
    |  min train  | fold 1 | fold 2 | ... | fold K      |                   |

* Walk-forward folds live entirely inside the development period and use an expanding
  training window.
* The holdout is evaluated exactly once, with a model trained on all development data.
* Hyperparameters are fixed in code before any evaluation (no tuning), so the
  walk-forward folds are diagnostics of stability, not a model-selection loop.

Purging: the label at ``t`` depends on prices up to ``t + delay + horizon``. A training
row whose label window reaches into the test period leaks test-period prices into
training. We therefore drop training rows with ``t >= test_start - purge`` where
``purge = (delay + horizon) * bar``. Training data always *precedes* test data here, so
no post-test embargo is needed (López de Prado's embargo addresses training rows that
follow a test block, which this layout never has).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Fold:
    name: str
    train: np.ndarray  # integer positions
    test: np.ndarray
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def purge_width(horizon: int, delay: int, bar: str = "1min") -> pd.Timedelta:
    return (horizon + delay) * pd.Timedelta(bar)


def _train_before(index: pd.DatetimeIndex, test_start_pos: int, purge: pd.Timedelta) -> np.ndarray:
    cutoff = index[test_start_pos] - purge
    return np.flatnonzero(index < cutoff)


def split_positions(n: int, holdout_frac: float, min_train_frac: float, n_folds: int):
    if not 0 < holdout_frac < 1 or not 0 < min_train_frac < 1:
        raise ValueError("fractions must be in (0, 1)")
    n_dev = int(round(n * (1 - holdout_frac)))
    first_test = int(round(n * min_train_frac))
    if first_test >= n_dev or n_folds < 1:
        raise ValueError("min_train_frac must leave room for walk-forward folds")
    edges = np.linspace(first_test, n_dev, n_folds + 1).round().astype(int)
    return n_dev, edges


def walk_forward(
    index: pd.DatetimeIndex,
    n_folds: int,
    holdout_frac: float,
    min_train_frac: float,
    purge: pd.Timedelta,
) -> list[Fold]:
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("index must be strictly increasing")
    _, edges = split_positions(len(index), holdout_frac, min_train_frac, n_folds)
    folds = []
    for k in range(n_folds):
        lo, hi = edges[k], edges[k + 1]
        test = np.arange(lo, hi)
        folds.append(
            Fold(
                name=f"wf{k + 1}",
                train=_train_before(index, lo, purge),
                test=test,
                test_start=index[lo],
                test_end=index[hi - 1],
            )
        )
    return folds


def holdout(
    index: pd.DatetimeIndex, holdout_frac: float, min_train_frac: float, purge: pd.Timedelta
) -> Fold:
    n_dev, _ = split_positions(len(index), holdout_frac, min_train_frac, 1)
    test = np.arange(n_dev, len(index))
    return Fold(
        name="holdout",
        train=_train_before(index, n_dev, purge),
        test=test,
        test_start=index[n_dev],
        test_end=index[-1],
    )


def dev_positions(n: int, holdout_frac: float) -> np.ndarray:
    return np.arange(int(round(n * (1 - holdout_frac))))
