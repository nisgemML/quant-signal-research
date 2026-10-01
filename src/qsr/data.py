"""Binance public kline data: download, checksum verification, loading, validation.

Source: https://data.binance.vision (spot, monthly archives). Every archive ships with a
``.CHECKSUM`` file (SHA-256). We verify it on download and again on every load, so a
results file can always be traced back to byte-identical inputs.

Two format details that matter for correctness:

* Spot kline timestamps switched from milliseconds to microseconds starting with the
  2025-01 archives. Units are detected per value, so mixed ranges load correctly.
* Some archives include a header row and some do not. Both are handled.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
]
KEEP_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "count",
    "taker_buy_base_volume",
]
FLOAT_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_base_volume",
]

# Epoch values at or above this are microseconds (ms epochs are ~1.7e12 in 2024-2026).
_MICROSECOND_THRESHOLD = 10**14
# Binance launched in July 2017; anything earlier means a timestamp-unit bug.
_EARLIEST_PLAUSIBLE = pd.Timestamp("2017-07-01", tz="UTC")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ChecksumError(RuntimeError):
    """Raised when an archive does not match its published SHA-256."""


class DataIntegrityError(RuntimeError):
    """Raised when loaded data is internally inconsistent (e.g. conflicting duplicates)."""


# --------------------------------------------------------------------------- paths / urls


def month_range(start: str, end: str) -> list[tuple[int, int]]:
    """Inclusive list of (year, month) between two 'YYYY-MM' strings."""
    periods = pd.period_range(start=start, end=end, freq="M")
    if len(periods) == 0:
        raise ValueError(f"empty month range: {start}..{end}")
    return [(p.year, p.month) for p in periods]


def archive_name(symbol: str, interval: str, year: int, month: int) -> str:
    return f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"


def archive_url(symbol: str, interval: str, year: int, month: int) -> str:
    return f"{BASE_URL}/{symbol}/{interval}/{archive_name(symbol, interval, year, month)}"


def archive_paths(data_dir: Path, symbol: str, interval: str, start: str, end: str) -> list[Path]:
    return [
        Path(data_dir) / archive_name(symbol, interval, y, m) for y, m in month_range(start, end)
    ]


# --------------------------------------------------------------------------- checksums


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksum(text: str) -> str:
    """Parse a Binance .CHECKSUM file ('<sha256>  <filename>')."""
    tokens = text.strip().split()
    if not tokens:
        raise ChecksumError("empty checksum file")
    digest = tokens[0].lower()
    if not _SHA256_RE.match(digest):
        raise ChecksumError(f"malformed checksum: {tokens[0]!r}")
    return digest


def verify_archive(path: Path) -> str:
    """Verify an archive against its sibling .CHECKSUM file. Returns the digest."""
    path = Path(path)
    checksum_path = path.with_name(path.name + ".CHECKSUM")
    if not checksum_path.exists():
        raise ChecksumError(f"missing checksum file for {path.name}; re-run the download")
    expected = parse_checksum(checksum_path.read_text())
    actual = sha256_file(path)
    if actual != expected:
        raise ChecksumError(f"{path.name}: sha256 {actual} != published {expected}")
    return actual


# --------------------------------------------------------------------------- download


def download_month(
    symbol: str,
    interval: str,
    year: int,
    month: int,
    dest: Path,
    session=None,
    timeout: float = 120.0,
) -> Path:
    """Download one monthly archive and verify it. Idempotent: skips verified files."""
    if session is None:
        import requests

        session = requests.Session()

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    name = archive_name(symbol, interval, year, month)
    url = archive_url(symbol, interval, year, month)
    zip_path = dest / name
    checksum_path = dest / (name + ".CHECKSUM")

    resp = session.get(url + ".CHECKSUM", timeout=timeout)
    resp.raise_for_status()
    checksum_text = resp.text
    expected = parse_checksum(checksum_text)

    if zip_path.exists() and sha256_file(zip_path) == expected:
        checksum_path.write_text(checksum_text)
        return zip_path

    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    part = dest / (name + ".part")
    part.write_bytes(resp.content)
    actual = sha256_file(part)
    if actual != expected:
        part.unlink()
        raise ChecksumError(f"{name}: downloaded sha256 {actual} != published {expected}")
    part.replace(zip_path)
    checksum_path.write_text(checksum_text)
    return zip_path


# --------------------------------------------------------------------------- loading


def epoch_to_utc(values: np.ndarray) -> pd.DatetimeIndex:
    """Convert Binance epoch integers (ms or us, detected per value) to UTC timestamps."""
    v = np.asarray(values, dtype=np.int64)
    ms = np.where(v >= _MICROSECOND_THRESHOLD, v // 1000, v)
    # Fix the resolution so behaviour is identical on pandas 2.x (ns) and 3.x (us default).
    return pd.DatetimeIndex(pd.to_datetime(ms, unit="ms", utc=True)).as_unit("ns")


def read_klines_zip(path: Path) -> pd.DataFrame:
    """Read one Binance kline archive into a DataFrame indexed by UTC open time."""
    with zipfile.ZipFile(path) as zf:
        members = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(members) != 1:
            raise DataIntegrityError(f"{path}: expected one csv, found {members}")
        raw_bytes = zf.read(members[0])

    raw = pd.read_csv(io.BytesIO(raw_bytes), header=None, dtype=str)
    first = str(raw.iat[0, 0]).strip()
    if not first.isdigit():  # header row present
        raw = raw.iloc[1:]
    if raw.shape[1] != len(KLINE_COLUMNS):
        raise DataIntegrityError(f"{path}: expected {len(KLINE_COLUMNS)} columns")
    raw.columns = KLINE_COLUMNS

    out = pd.DataFrame(index=epoch_to_utc(raw["open_time"].astype(np.int64).to_numpy()))
    out.index.name = "open_time"
    for col in FLOAT_COLUMNS:
        out[col] = raw[col].astype(np.float64).to_numpy()
    out["count"] = raw["count"].astype(np.int64).to_numpy()
    return out[KEEP_COLUMNS]


def load_klines(paths: list[Path], verify: bool = True) -> tuple[pd.DataFrame, dict[str, str]]:
    """Load and concatenate archives. Returns (frame, {filename: sha256}).

    Exact duplicate rows are dropped; duplicates with conflicting values raise.
    """
    frames, digests = [], {}
    for p in paths:
        p = Path(p)
        if not p.exists():
            raise FileNotFoundError(f"{p} not found - run `make data` first")
        digests[p.name] = verify_archive(p) if verify else sha256_file(p)
        frames.append(read_klines_zip(p))

    df = pd.concat(frames).sort_index(kind="stable")
    if df.index[0] < _EARLIEST_PLAUSIBLE:
        # Fail loudly: a unit error here would otherwise build a decades-long empty grid.
        raise DataIntegrityError(f"implausible timestamp {df.index[0]}; check epoch units")
    n_before = len(df)
    dup_mask = df.index.duplicated(keep=False)
    if dup_mask.any():
        conflicting = df[dup_mask].groupby(level=0).nunique().gt(1).any(axis=1)
        if conflicting.any():
            ts = conflicting[conflicting].index[0]
            raise DataIntegrityError(f"conflicting duplicate bars, first at {ts}")
        df = df[~df.index.duplicated(keep="first")]
    df.attrs["duplicates_dropped"] = n_before - len(df)
    return df, digests


# --------------------------------------------------------------------------- validation


@dataclass
class DataQualityReport:
    n_rows: int
    start: str
    end: str
    expected_bars: int
    missing_bars: int
    missing_pct: float
    duplicates_dropped: int
    zero_volume_bars: int
    ohlc_violations: int
    taker_volume_violations: int
    largest_gaps: list[tuple[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def validate_klines(df: pd.DataFrame, freq: str = "1min", top_gaps: int = 5) -> DataQualityReport:
    """Summarize data quality. Does not modify the data."""
    step = pd.Timedelta(freq)
    full = pd.date_range(df.index[0], df.index[-1], freq=freq, tz="UTC")
    missing = full.difference(df.index)

    gaps: list[tuple[str, int]] = []
    if len(df) > 1:
        deltas = pd.Series(df.index[1:] - df.index[:-1], index=df.index[:-1])
        big = deltas[deltas > step].sort_values(ascending=False).head(top_gaps)
        gaps = [(str(ts), int(d / step) - 1) for ts, d in big.items()]

    body_hi = df[["open", "close"]].max(axis=1)
    body_lo = df[["open", "close"]].min(axis=1)
    ohlc_bad = (df["high"] < body_hi) | (df["low"] > body_lo) | (df["low"] <= 0)

    return DataQualityReport(
        n_rows=len(df),
        start=str(df.index[0]),
        end=str(df.index[-1]),
        expected_bars=len(full),
        missing_bars=len(missing),
        missing_pct=round(100.0 * len(missing) / len(full), 4),
        duplicates_dropped=int(df.attrs.get("duplicates_dropped", 0)),
        zero_volume_bars=int((df["volume"] == 0).sum()),
        ohlc_violations=int(ohlc_bad.sum()),
        taker_volume_violations=int(
            (df["taker_buy_base_volume"] > df["volume"] * (1 + 1e-9)).sum()
        ),
        largest_gaps=gaps,
    )


def regularize(df: pd.DataFrame, freq: str = "1min") -> pd.DataFrame:
    """Reindex onto a complete time grid. Missing bars become NaN - never forward-filled.

    Features and labels are computed on this grid so that every shift is a shift in
    *time*, not in rows. A window that touches a missing bar yields NaN and is dropped.
    """
    full = pd.date_range(df.index[0], df.index[-1], freq=freq, tz="UTC", name=df.index.name)
    return df.reindex(full)


# --------------------------------------------------------------------------- CLI


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Download Binance spot kline archives.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--symbol", default="BTCUSDT")
    d.add_argument("--interval", default="1m")
    d.add_argument("--start", required=True, help="YYYY-MM")
    d.add_argument("--end", required=True, help="YYYY-MM")
    d.add_argument("--dest", default="data/raw")
    args = parser.parse_args()

    for year, month in month_range(args.start, args.end):
        path = download_month(args.symbol, args.interval, year, month, Path(args.dest))
        print(f"ok  {path.name}  sha256={sha256_file(path)[:16]}...")


if __name__ == "__main__":
    _cli()
