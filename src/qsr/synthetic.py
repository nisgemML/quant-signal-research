"""Synthetic klines in Binance's exact archive format. FOR TESTS AND CI ONLY.

Nothing in the study's results is computed from this module. It exists so that tests
and CI run offline and deterministically, and so the pipeline can be checked for
*size* (pure noise is not called a signal) and *power* (a planted signal is found).
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from qsr.data import KLINE_COLUMNS, archive_name


def synthetic_klines(
    start: str = "2024-01-01",
    periods: int = 20_000,
    seed: int = 0,
    signal: float = 0.0,
    sigma: float = 5e-4,
) -> pd.DataFrame:
    """1-minute bars. If ``signal`` > 0, taker imbalance at t predicts the return at t+1."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=periods, freq="1min", tz="UTC", name="open_time")

    imb = np.zeros(periods)
    shocks = rng.normal(0, 0.35, periods)
    for t in range(1, periods):
        imb[t] = np.clip(0.3 * imb[t - 1] + shocks[t], -0.95, 0.95)

    rets = sigma * rng.standard_normal(periods)
    rets[1:] += signal * sigma * imb[:-1] / imb.std()
    close = 30_000 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, sigma / 2, (2, periods)))
    high = np.maximum(open_, close) * np.exp(wick[0])
    low = np.minimum(open_, close) * np.exp(-wick[1])
    volume = rng.lognormal(mean=2.0, sigma=0.6, size=periods)
    taker_buy = volume * (1 + imb) / 2

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "quote_volume": volume * close,
            "count": rng.integers(50, 500, periods),
            "taker_buy_base_volume": taker_buy,
        },
        index=idx,
    )


def to_binance_csv(df: pd.DataFrame, microseconds: bool = False, header: bool = False) -> bytes:
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    ms = ((df.index - epoch) // pd.Timedelta("1ms")).to_numpy(dtype=np.int64)
    scale = 1000 if microseconds else 1
    step = 60_000 * scale
    open_time = ms * scale
    raw = pd.DataFrame(
        {
            "open_time": open_time,
            "open": df["open"],
            "high": df["high"],
            "low": df["low"],
            "close": df["close"],
            "volume": df["volume"],
            "close_time": open_time + step - 1,
            "quote_volume": df["quote_volume"],
            "count": df["count"],
            "taker_buy_base_volume": df["taker_buy_base_volume"],
            "taker_buy_quote_volume": df["taker_buy_base_volume"] * df["close"],
            "ignore": 0,
        }
    )[KLINE_COLUMNS]
    buf = io.StringIO()
    raw.to_csv(buf, index=False, header=header, float_format="%.10g")
    return buf.getvalue().encode()


def write_archive(
    df: pd.DataFrame, path: Path, microseconds: bool = False, header: bool = False
) -> Path:
    """Write a zip + .CHECKSUM pair exactly as data.binance.vision publishes them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(path.name.replace(".zip", ".csv"), to_binance_csv(df, microseconds, header))
    path.write_bytes(buf.getvalue())
    digest = hashlib.sha256(buf.getvalue()).hexdigest()
    path.with_name(path.name + ".CHECKSUM").write_text(f"{digest}  {path.name}\n")
    return path


def write_monthly_archives(
    df: pd.DataFrame, data_dir: Path, symbol: str = "BTCUSDT", us_from: str | None = None
) -> list[Path]:
    """Split by calendar month; months >= ``us_from`` use microsecond timestamps."""
    paths = []
    for period, chunk in df.groupby(df.index.tz_localize(None).to_period("M")):
        name = archive_name(symbol, "1m", period.year, period.month)
        us = us_from is not None and str(period) >= us_from
        paths.append(write_archive(chunk, data_dir / name, microseconds=us))
    return paths
