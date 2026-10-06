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


def _weak_signal(n: int, seed: int = 1):
    rng = np.random.default_rng(seed)
    s = rng.standard_normal(n)
    for i in range(1, n):  # serially dependent, like real features
        s[i] = 0.9 * s[i - 1] + 0.436 * s[i]
    fwd = 0.02 * s / s.std() + rng.standard_normal(n)
    return (fwd > 0).astype(float), 1 / (1 + np.exp(-0.05 * s)), fwd


def test_normal_intervals_are_stable_at_bonferroni_level():
    """At the Bonferroni level (~0.997) a percentile interval from a few hundred draws
    rests on ~0.4 draws per tail, so its endpoint moves with the seed; the normal
    (bootstrap-SE) interval should not. Measured in METHODOLOGY.md: 0.0023 vs 0.0005 spread."""
    y, p, fwd = _weak_signal(12_000)
    level = ev.bonferroni_level(18)
    spread = {}
    for method in ("percentile", "normal"):
        los = [
            ev.bootstrap_intervals(y, p, fwd, 60, 200, level, seed=s, method=method)["auc"].lo
            for s in range(4)
        ]
        spread[method] = max(los) - min(los)
    assert spread["normal"] < spread["percentile"]


def test_normal_interval_is_centred_on_point_estimate():
    y, p, fwd = _weak_signal(5_000)
    ci = ev.bootstrap_intervals(y, p, fwd, 60, 200, 0.95, seed=0)["auc"]
    assert abs((ci.lo + ci.hi) / 2 - ev._auc(y, p)) < 1e-12
    assert ci.lo < ev._auc(y, p) < ci.hi


def test_unknown_interval_method_rejected():
    y, p, fwd = _weak_signal(500)
    try:
        ev.bootstrap_intervals(y, p, fwd, 30, 10, 0.95, method="bca")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
