"""Metrics, block-bootstrap confidence intervals, and the verdict rule.

Minute returns are autocorrelated in their magnitude (volatility clustering), and
overlapping h-minute labels are autocorrelated by construction. An i.i.d. bootstrap or a
naive t-stat would overstate confidence, so all intervals use a moving-block bootstrap
with blocks much longer than the label horizon.

Metrics:

* ``auc``         - ROC AUC on non-flat outcomes. 0.5 = no ranking ability.
* ``accuracy``    - hit rate of p > 0.5 on non-flat outcomes. Reported alongside the
                    test-period majority rate so it is never read in isolation.
* ``ll_skill``    - 1 - logloss(model) / logloss(train base rate). > 0 means better
                    calibrated probabilities than the naive prior.
* ``rank_ic``     - Spearman correlation between predicted probability and the forward
                    return, over all rows (flat outcomes included).
* ``edge_bps``    - mean of sign(p - 0.5) * forward log return, in basis points: the
                    average gross return per prediction if each prediction were a
                    separate round-trip trade at the label's entry and exit prices.
                    This is a cost hurdle check, NOT a backtest (no fills, queue
                    position, latency, market impact, or position netting).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import rankdata
from sklearn.metrics import log_loss, roc_auc_score

EPS = 1e-6


def _rank_ic(p: np.ndarray, fwd: np.ndarray) -> float:
    if np.ptp(p) == 0 or np.ptp(fwd) == 0:
        return float("nan")
    return float(np.corrcoef(rankdata(p), rankdata(fwd))[0, 1])


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    m = ~np.isnan(y)
    yy = y[m]
    if yy.size == 0 or yy.min() == yy.max():
        return float("nan")
    return float(roc_auc_score(yy, p[m]))


def _edge_bps(p: np.ndarray, fwd: np.ndarray) -> float:
    return float(np.mean(np.sign(p - 0.5) * fwd) * 1e4)


def point_metrics(y: np.ndarray, p: np.ndarray, fwd: np.ndarray, train_base_rate: float) -> dict:
    y, p, fwd = (np.asarray(a, dtype=float) for a in (y, p, fwd))
    m = ~np.isnan(y)
    yy, pp = y[m], np.clip(p[m], EPS, 1 - EPS)
    up_rate = float(yy.mean()) if yy.size else float("nan")
    if yy.size and yy.min() != yy.max():
        ll = log_loss(yy, pp, labels=[0.0, 1.0])
        base = np.full_like(pp, np.clip(train_base_rate, EPS, 1 - EPS))
        ll_base = log_loss(yy, base, labels=[0.0, 1.0])
        ll_skill = float(1.0 - ll / ll_base)
    else:
        ll_skill = float("nan")
    return {
        "n": int(p.size),
        "n_nonflat": int(m.sum()),
        "test_up_rate": up_rate,
        "test_majority_acc": max(up_rate, 1 - up_rate) if yy.size else float("nan"),
        "accuracy": float(np.mean((pp > 0.5) == (yy == 1.0))) if yy.size else float("nan"),
        "auc": _auc(y, p),
        "ll_skill": ll_skill,
        "rank_ic": _rank_ic(p, fwd),
        "edge_bps": _edge_bps(p, fwd),
        "frac_long": float(np.mean(p > 0.5)),
    }


def block_indices(n: int, block_len: int, n_boot: int, rng: np.random.Generator) -> np.ndarray:
    """Moving-block bootstrap: each row of the result is one resample of positions 0..n-1."""
    block_len = int(min(max(block_len, 1), n))
    n_blocks = int(np.ceil(n / block_len))
    starts = rng.integers(0, n - block_len + 1, size=(n_boot, n_blocks))
    idx = starts[:, :, None] + np.arange(block_len)[None, None, :]
    return idx.reshape(n_boot, -1)[:, :n]


@dataclass
class Interval:
    lo: float
    hi: float


def bootstrap_intervals(
    y: np.ndarray,
    p: np.ndarray,
    fwd: np.ndarray,
    block_len: int,
    n_boot: int,
    level: float,
    seed: int = 0,
    method: str = "normal",
) -> dict[str, Interval]:
    """Moving-block bootstrap intervals for AUC, rank IC and edge.

    ``method="normal"`` (default): point estimate +/- z * SD of the bootstrap draws.
    ``method="percentile"``: empirical quantiles of the draws.

    Why normal is the default: the Bonferroni-adjusted level here is ~0.9972, so a
    percentile interval needs the 0.14% and 99.86% quantiles of the bootstrap draws.
    With n_boot = 300 that is ~0.4 draws per tail, so each endpoint is essentially the
    min or max of the draws. Measured on a planted weak signal (see METHODOLOGY.md,
    "Interval construction"): across 6 seeds the 300-draw percentile lower bound ranged
    0.4982-0.5005, straddling 0.5, so the verdict depended on the seed; with 2,000 draws
    it was 0.4975-0.4987, consistently lower, i.e. 300-draw percentile intervals were
    too NARROW (anti-conservative). A standard deviation is estimated well from a few
    hundred draws; an extreme quantile is not. AUC, rank IC and mean edge are averages
    over ~10^5 bars, so their sampling distributions are close to normal.
    """
    from statistics import NormalDist

    if method not in ("normal", "percentile"):
        raise ValueError(f"unknown interval method {method!r}")
    y, p, fwd = (np.asarray(a, dtype=float) for a in (y, p, fwd))
    point = {"auc": _auc(y, p), "rank_ic": _rank_ic(p, fwd), "edge_bps": _edge_bps(p, fwd)}
    rng = np.random.default_rng(seed)
    idx = block_indices(len(p), block_len, n_boot, rng)
    stats = {"auc": [], "rank_ic": [], "edge_bps": []}
    for row in idx:
        yy, pp, ff = y[row], p[row], fwd[row]
        stats["auc"].append(_auc(yy, pp))
        stats["rank_ic"].append(_rank_ic(pp, ff))
        stats["edge_bps"].append(_edge_bps(pp, ff))
    alpha = 1.0 - level
    z = NormalDist().inv_cdf(1.0 - alpha / 2)
    out = {}
    for k, v in stats.items():
        arr = np.asarray(v, dtype=float)
        arr = arr[~np.isnan(arr)]
        if arr.size < 2 or np.isnan(point[k]):
            out[k] = Interval(float("nan"), float("nan"))
        elif method == "normal":
            half = z * float(np.std(arr, ddof=1))
            out[k] = Interval(point[k] - half, point[k] + half)
        else:
            lo, hi = np.quantile(arr, [alpha / 2, 1 - alpha / 2])
            out[k] = Interval(float(lo), float(hi))
    return out


def default_block_len(horizon: int, delay: int) -> int:
    """At least an hour of bars, and at least 20x the label span."""
    return max(60, 20 * (horizon + delay))


# --------------------------------------------------------------------------- verdict

NO_SIGNAL = "no detectable signal"
BELOW_COST = "detectable, below cost hurdle"
ABOVE_COST = "detectable, above cost hurdle"
BASELINE = "baseline"


def verdict(
    auc_ci: Interval, edge_ci: Interval, round_trip_cost_bps: float, is_baseline: bool
) -> str:
    """Mechanical verdict so no result is described more strongly than the data allow.

    * Statistically detectable: the (Bonferroni-adjusted) AUC interval excludes 0.5.
    * Above cost hurdle: additionally, the lower edge bound exceeds the round-trip cost.
    """
    if is_baseline:
        return BASELINE
    if not (auc_ci.lo > 0.5):
        return NO_SIGNAL
    if edge_ci.lo > round_trip_cost_bps:
        return ABOVE_COST
    return BELOW_COST


def bonferroni_level(n_tests: int, family_alpha: float = 0.05) -> float:
    return 1.0 - family_alpha / max(n_tests, 1)
