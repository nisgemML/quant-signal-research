"""Every feature must be backward-looking. The checker itself must catch real leaks."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qsr.data import regularize
from qsr.features import (
    FEATURES,
    build_features,
    check_no_lookahead,
    direction_label,
    forward_log_return,
    make_dataset,
)
from qsr.synthetic import synthetic_klines

CUTS = [1500, 1501, 2222, 3000, 3999, 4500]


@pytest.fixture(scope="module")
def grid():
    return regularize(synthetic_klines(periods=5_000, seed=7))


@pytest.mark.parametrize("name", sorted(FEATURES))
def test_feature_is_backward_looking(grid, name):
    assert check_no_lookahead(FEATURES[name], grid, CUTS) == []


# The checker is only useful if it fails on the bugs that actually happen in practice.
LEAKY = {
    "negative shift": lambda df: np.log(df["close"]).shift(-1),
    "centered rolling mean": lambda df: df["close"].rolling(21, center=True).mean(),
    "full-sample z-score": lambda df: (df["close"] - df["close"].mean()) / df["close"].std(),
    "forward-filled from future (bfill)": lambda df: (
        df["close"].where(df.index.minute == 0).bfill()
    ),
}


@pytest.mark.parametrize("name", sorted(LEAKY))
def test_checker_catches_known_leaks(grid, name):
    assert check_no_lookahead(LEAKY[name], grid, CUTS) != []


def test_past_return_and_imbalance_values(grid):
    f = build_features(grid)
    t = 3000
    expected = np.log(grid["close"].iloc[t] / grid["close"].iloc[t - 5])
    assert f["ret_5"].iloc[t] == pytest.approx(expected)
    imb = f[["taker_imb_1", "taker_imb_15", "taker_imb_60"]].dropna()
    assert imb.abs().le(1.0 + 1e-12).all().all()


def test_windows_touching_a_missing_bar_are_nan():
    df = synthetic_klines(periods=3_000, seed=3)
    df = df.drop(df.index[2000])
    f = build_features(regularize(df))
    # bar 2000 is missing, so the 1-bar returns at 2000 and 2001 are unknown; every
    # 15-bar window containing either of them must be NaN, and none after.
    assert f["rv_15"].iloc[2000:2016].isna().all()
    assert f["rv_15"].iloc[2016:2030].notna().all()


def test_forward_return_alignment_with_and_without_delay():
    close = pd.Series(np.exp(np.arange(20, dtype=float) * 0.01))
    lc = np.log(close)
    f0 = forward_log_return(close, horizon=3, delay=0)
    f1 = forward_log_return(close, horizon=3, delay=1)
    assert f0.iloc[5] == pytest.approx(lc.iloc[8] - lc.iloc[5])
    assert f1.iloc[5] == pytest.approx(lc.iloc[9] - lc.iloc[6])
    assert f0.iloc[-3:].isna().all() and f1.iloc[-4:].isna().all()


def test_labels_flat_is_nan():
    y = direction_label(pd.Series([0.01, -0.02, 0.0, np.nan]))
    assert y.iloc[0] == 1.0 and y.iloc[1] == 0.0 and np.isnan(y.iloc[2]) and np.isnan(y.iloc[3])


def test_forward_return_is_shifted_in_time_not_rows():
    full = synthetic_klines(periods=3_000, seed=4)
    df = full.drop(full.index[2500])
    fwd = forward_log_return(regularize(df)["close"], horizon=5)
    lc = np.log(full["close"])
    # endpoints on the missing bar are unknown...
    assert np.isnan(fwd.iloc[2495]) and np.isnan(fwd.iloc[2500])
    # ...rows that merely straddle it are still exact 5-minute returns
    assert fwd.iloc[2497] == pytest.approx(lc.iloc[2502] - lc.iloc[2497])
    # a row-based shift on the raw frame would silently return a 6-minute return here
    row_based = np.log(df["close"]).shift(-5) - np.log(df["close"])
    assert row_based.iloc[2497] == pytest.approx(lc.iloc[2503] - lc.iloc[2497])


def test_dataset_rows_are_complete(grid):
    ds = make_dataset(grid, build_features(grid), horizon=5, delay=1)
    assert ds.X.notna().all().all() and ds.fwd.notna().all()
    assert len(ds.X) == len(ds.y) == len(ds.fwd)
