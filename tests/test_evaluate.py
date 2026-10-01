"""Statistics: intervals have the right size under the null and power under a real signal."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qsr import evaluate as ev
from qsr.data import regularize
from qsr.features import build_features, make_dataset
from qsr.report import _fit_predict
from qsr.synthetic import synthetic_klines
from qsr.validation import holdout, purge_width


def test_block_indices_shape_and_range():
    rng = np.random.default_rng(0)
    idx = ev.block_indices(1000, 60, 50, rng)
    assert idx.shape == (50, 1000) and idx.min() >= 0 and idx.max() < 1000


def test_bootstrap_interval_covers_true_mean_on_iid_data():
    rng = np.random.default_rng(1)
    covered = 0
    for s in range(40):
        fwd = rng.normal(0.0, 1e-3, 3000)
        p = np.full(3000, 0.6)  # always long -> edge = mean(fwd)
        ci = ev.bootstrap_intervals(np.full(3000, np.nan), p, fwd, 30, 200, 0.95, seed=s)
        covered += ci["edge_bps"].lo <= 0.0 <= ci["edge_bps"].hi
    assert covered >= 34  # ~95% nominal; loose bound keeps the test stable


def _holdout_auc_ci(signal: float, seed: int):
    grid = regularize(synthetic_klines(periods=30_000, seed=seed, signal=signal))
    ds = make_dataset(grid, build_features(grid), horizon=1, delay=0)
    fold = holdout(pd.DatetimeIndex(ds.X.index), 0.3, 0.3, purge_width(1, 0))
    p, _ = _fit_predict("logit_all", ds.X, ds.y, fold.train, fold.test)
    y, fwd = ds.y.to_numpy()[fold.test], ds.fwd.to_numpy()[fold.test]
    return ev.bootstrap_intervals(y, p, fwd, 60, 200, 0.99, seed=seed)["auc"]


def test_no_signal_is_not_detected():
    ci = _holdout_auc_ci(signal=0.0, seed=11)
    assert ci.lo <= 0.5 <= ci.hi


def test_planted_signal_is_detected():
    ci = _holdout_auc_ci(signal=0.4, seed=12)
    assert ci.lo > 0.5


def test_verdict_rule():
    iv = ev.Interval
    assert ev.verdict(iv(0.49, 0.53), iv(5, 30), 20, False) == ev.NO_SIGNAL
    assert ev.verdict(iv(0.51, 0.53), iv(1, 30), 20, False) == ev.BELOW_COST
    assert ev.verdict(iv(0.51, 0.53), iv(21, 30), 20, False) == ev.ABOVE_COST
    assert ev.verdict(iv(0.51, 0.53), iv(21, 30), 20, True) == ev.BASELINE


def test_point_metrics_on_perfect_and_useless_predictions():
    fwd = np.array([0.01, -0.01, 0.02, -0.02, 0.0])
    y = np.array([1.0, 0.0, 1.0, 0.0, np.nan])
    perfect = ev.point_metrics(y, np.array([0.9, 0.1, 0.9, 0.1, 0.5]), fwd, 0.5)
    assert perfect["auc"] == 1.0 and perfect["accuracy"] == 1.0 and perfect["edge_bps"] > 0
    flat = ev.point_metrics(y, np.full(5, 0.5), fwd, 0.5)
    assert flat["auc"] == 0.5 and flat["edge_bps"] == 0.0
