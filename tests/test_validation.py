"""Splits are chronological, purged, and never let test-period prices into training."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qsr.validation import holdout, purge_width, walk_forward


def _index_with_gaps(n=10_000, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    keep = rng.random(n) > 0.05
    return idx[keep]


@pytest.mark.parametrize("horizon,delay", [(1, 0), (5, 1), (15, 0)])
def test_walk_forward_is_purged_and_chronological(horizon, delay):
    idx = _index_with_gaps()
    purge = purge_width(horizon, delay)
    folds = walk_forward(idx, n_folds=4, holdout_frac=0.2, min_train_frac=0.3, purge=purge)
    ho = holdout(idx, 0.2, 0.3, purge)
    prev_end = -1
    for f in folds:
        # label window of the last training row ends strictly before the test period
        last_train_label_end = idx[f.train[-1]] + purge
        assert last_train_label_end < idx[f.test[0]]
        assert f.test[0] == prev_end + 1 or prev_end == -1
        assert f.test[-1] < ho.test[0]
        prev_end = f.test[-1]
    assert prev_end + 1 == ho.test[0]  # folds tile the development period
    assert idx[ho.train[-1]] + purge < idx[ho.test[0]]
    assert ho.test[-1] == len(idx) - 1


def test_training_windows_expand():
    idx = _index_with_gaps()
    folds = walk_forward(idx, 5, 0.2, 0.3, purge_width(5, 0))
    sizes = [len(f.train) for f in folds]
    assert sizes == sorted(sizes) and all(f.train[0] == 0 for f in folds)


def test_rejects_unsorted_index():
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-01"], tz="UTC")
    with pytest.raises(ValueError):
        walk_forward(idx, 1, 0.2, 0.3, purge_width(1, 0))
