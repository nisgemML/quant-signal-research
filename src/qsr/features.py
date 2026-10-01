"""Features, labels, and the lookahead checker.

Timing convention (the single most important thing in this file):

* A feature at bar ``t`` may use information up to and including the **close of bar t**.
* The label at bar ``t`` is the log return from ``close[t + delay]`` to
  ``close[t + delay + horizon]``. ``delay=0`` assumes you can act at the close you just
  observed; ``delay=1`` assumes one bar of latency. Both are evaluated.

All computations run on a regular time grid (see ``data.regularize``), so shifts are in
time rather than rows, and any window touching a missing bar yields NaN.

Every feature is registered in ``FEATURES``. ``tests/test_features.py`` runs
``check_no_lookahead`` over the whole registry, so a feature cannot be added without
being proven backward-looking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

FeatureFn = Callable[[pd.DataFrame], pd.Series]
FEATURES: dict[str, FeatureFn] = {}


def _register(name: str, fn: FeatureFn) -> None:
    if name in FEATURES:
        raise ValueError(f"duplicate feature {name}")
    FEATURES[name] = fn


def _log_close(df: pd.DataFrame) -> pd.Series:
    return np.log(df["close"])


# --- past returns ------------------------------------------------------------------------


def _make_past_return(k: int) -> FeatureFn:
    def fn(df: pd.DataFrame) -> pd.Series:
        return _log_close(df).diff(k)

    return fn


for _k in (1, 5, 15, 60):
    _register(f"ret_{_k}", _make_past_return(_k))


# --- realized and range-based volatility ------------------------------------------------


def _make_realized_vol(w: int) -> FeatureFn:
    def fn(df: pd.DataFrame) -> pd.Series:
        r2 = _log_close(df).diff() ** 2
        return np.sqrt(r2.rolling(w, min_periods=w).sum())

    return fn


for _w in (15, 60):
    _register(f"rv_{_w}", _make_realized_vol(_w))


def _parkinson_60(df: pd.DataFrame) -> pd.Series:
    hl2 = np.log(df["high"] / df["low"]) ** 2
    return np.sqrt(hl2.rolling(60, min_periods=60).mean() / (4.0 * np.log(2.0)))


_register("parkinson_60", _parkinson_60)


# --- order flow: taker imbalance -------------------------------------------------------
# Signed taker flow fraction in [-1, 1]: (buy - sell) / total, where taker sell volume is
# volume - taker_buy_volume.


def _make_taker_imbalance(w: int) -> FeatureFn:
    def fn(df: pd.DataFrame) -> pd.Series:
        signed = 2.0 * df["taker_buy_base_volume"] - df["volume"]
        num = signed.rolling(w, min_periods=w).sum()
        den = df["volume"].rolling(w, min_periods=w).sum()
        return (num / den).where(den > 0)

    return fn


for _w in (1, 15, 60):
    _register(f"taker_imb_{_w}", _make_taker_imbalance(_w))


# --- activity and position in range -------------------------------------------------------


def _volume_z_1440(df: pd.DataFrame) -> pd.Series:
    lv = np.log1p(df["volume"])
    mean = lv.rolling(1440, min_periods=1440).mean()
    std = lv.rolling(1440, min_periods=1440).std()
    return ((lv - mean) / std).where(std > 0)


def _trade_count_z_1440(df: pd.DataFrame) -> pd.Series:
    lc = np.log1p(df["count"].astype(float))
    mean = lc.rolling(1440, min_periods=1440).mean()
    std = lc.rolling(1440, min_periods=1440).std()
    return ((lc - mean) / std).where(std > 0)


def _range_position_60(df: pd.DataFrame) -> pd.Series:
    hi = df["high"].rolling(60, min_periods=60).max()
    lo = df["low"].rolling(60, min_periods=60).min()
    width = hi - lo
    return ((df["close"] - lo) / width).where(width > 0)


_register("volume_z_1440", _volume_z_1440)
_register("count_z_1440", _trade_count_z_1440)
_register("range_pos_60", _range_position_60)


# --- time of day (deterministic, known in advance) ------------------------------------


def _minute_of_day(df: pd.DataFrame) -> np.ndarray:
    idx = pd.DatetimeIndex(df.index)
    return (idx.hour * 60 + idx.minute).to_numpy(dtype=float)


def _tod_sin(df: pd.DataFrame) -> pd.Series:
    return pd.Series(np.sin(2 * np.pi * _minute_of_day(df) / 1440.0), index=df.index)


def _tod_cos(df: pd.DataFrame) -> pd.Series:
    return pd.Series(np.cos(2 * np.pi * _minute_of_day(df) / 1440.0), index=df.index)


_register("tod_sin", _tod_sin)
_register("tod_cos", _tod_cos)


def build_features(df: pd.DataFrame, names: list[str] | None = None) -> pd.DataFrame:
    names = list(FEATURES) if names is None else names
    out = pd.DataFrame({n: FEATURES[n](df) for n in names}, index=df.index)
    return out.replace([np.inf, -np.inf], np.nan)


# --------------------------------------------------------------------------- labels


def forward_log_return(close: pd.Series, horizon: int, delay: int = 0) -> pd.Series:
    """log(close[t + delay + horizon] / close[t + delay]). Requires a regular grid."""
    if horizon < 1 or delay < 0:
        raise ValueError("horizon must be >= 1 and delay >= 0")
    lc = np.log(close)
    return lc.shift(-(delay + horizon)) - lc.shift(-delay)


def direction_label(fwd: pd.Series) -> pd.Series:
    """1.0 if up, 0.0 if down, NaN if flat or unknown.

    Flat outcomes are common at 1-minute horizons. They are excluded from classification
    metrics but kept for return-based metrics (IC, gross edge), where they contribute 0.
    """
    lab = pd.Series(np.nan, index=fwd.index)
    lab[fwd > 0] = 1.0
    lab[fwd < 0] = 0.0
    return lab


@dataclass
class Dataset:
    X: pd.DataFrame
    y: pd.Series  # 1.0 / 0.0 / NaN (flat)
    fwd: pd.Series  # forward log return
    horizon: int
    delay: int
    n_grid: int
    n_dropped_incomplete: int
    n_flat: int


def make_dataset(grid: pd.DataFrame, features: pd.DataFrame, horizon: int, delay: int) -> Dataset:
    """Align features with labels on the regular grid, then drop incomplete rows."""
    fwd = forward_log_return(grid["close"], horizon, delay)
    ok = features.notna().all(axis=1) & fwd.notna()
    y = direction_label(fwd[ok])
    return Dataset(
        X=features[ok],
        y=y,
        fwd=fwd[ok],
        horizon=horizon,
        delay=delay,
        n_grid=len(grid),
        n_dropped_incomplete=int((~ok).sum()),
        n_flat=int(y.isna().sum()),
    )


# --------------------------------------------------------------------------- lookahead check


def check_no_lookahead(
    fn: FeatureFn,
    df: pd.DataFrame,
    cut_points: list[int],
    rtol: float = 1e-10,
    atol: float = 1e-12,
) -> list[int]:
    """Return the cut points at which ``fn`` is NOT backward-looking.

    For each cut ``c``, the feature is computed on the full frame and on ``df[:c+1]``.
    If any value at rows <= c differs, the feature used data from after ``c``.
    Empty list means no lookahead was detected.
    """
    full = pd.Series(fn(df), index=df.index).to_numpy(dtype=float)
    violations = []
    for c in cut_points:
        part = pd.Series(fn(df.iloc[: c + 1]), index=df.index[: c + 1]).to_numpy(dtype=float)
        head = full[: c + 1]
        both_nan = np.isnan(head) & np.isnan(part)
        close = np.isclose(head, part, rtol=rtol, atol=atol) | both_nan
        if not close.all():
            violations.append(c)
    return violations
