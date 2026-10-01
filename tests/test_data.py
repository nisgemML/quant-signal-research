"""Data loading: formats, checksums, duplicates, quality report."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pytest

from qsr import data as qd
from qsr.synthetic import synthetic_klines, write_archive


def test_millisecond_and_microsecond_archives_load_identically(tmp_path):
    df = synthetic_klines(periods=500)
    a = write_archive(df.iloc[:250], tmp_path / "BTCUSDT-1m-2024-12.zip", microseconds=False)
    b = write_archive(df.iloc[250:], tmp_path / "BTCUSDT-1m-2025-01.zip", microseconds=True)
    loaded, digests = qd.load_klines([a, b])
    assert (loaded.index == df.index).all()
    np.testing.assert_allclose(loaded["close"], df["close"], rtol=1e-9)
    assert set(digests) == {a.name, b.name}


def test_header_row_is_detected(tmp_path):
    df = synthetic_klines(periods=100)
    p = write_archive(df, tmp_path / "BTCUSDT-1m-2024-01.zip", header=True)
    loaded = qd.read_klines_zip(p)
    assert len(loaded) == 100
    assert loaded.index[0] == df.index[0]


def test_tampered_archive_fails_verification(tmp_path):
    p = write_archive(synthetic_klines(periods=50), tmp_path / "BTCUSDT-1m-2024-01.zip")
    p.write_bytes(p.read_bytes() + b"x")
    with pytest.raises(qd.ChecksumError):
        qd.load_klines([p])


def test_missing_checksum_file_fails(tmp_path):
    p = write_archive(synthetic_klines(periods=50), tmp_path / "BTCUSDT-1m-2024-01.zip")
    p.with_name(p.name + ".CHECKSUM").unlink()
    with pytest.raises(qd.ChecksumError):
        qd.load_klines([p])


def test_exact_duplicates_dropped_conflicting_duplicates_raise(tmp_path):
    df = synthetic_klines(periods=100)
    a = write_archive(df.iloc[:60], tmp_path / "BTCUSDT-1m-2024-01.zip")
    b = write_archive(df.iloc[50:], tmp_path / "BTCUSDT-1m-2024-02.zip")
    loaded, _ = qd.load_klines([a, b])
    assert len(loaded) == 100
    assert loaded.attrs["duplicates_dropped"] == 10

    bad = df.iloc[50:].copy()
    bad.iloc[0, bad.columns.get_loc("close")] *= 1.01
    c = write_archive(bad, tmp_path / "BTCUSDT-1m-2024-03.zip")
    with pytest.raises(qd.DataIntegrityError):
        qd.load_klines([a, c])


def test_quality_report_counts_gaps_and_violations():
    df = synthetic_klines(periods=1_000)
    df = df.drop(df.index[100:130])  # 30-minute outage
    df.iloc[5, df.columns.get_loc("high")] = df["low"].iloc[5] * 0.5
    rep = qd.validate_klines(df)
    assert rep.missing_bars == 30
    assert rep.largest_gaps[0][1] == 30
    assert rep.ohlc_violations == 1


def test_regularize_leaves_missing_bars_as_nan():
    df = synthetic_klines(periods=200).drop(
        pd.date_range("2024-01-01 01:00", periods=5, freq="1min", tz="UTC")
    )
    grid = qd.regularize(df)
    assert len(grid) == 200
    assert grid["close"].isna().sum() == 5


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.text = content.decode(errors="replace")

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, zip_bytes: bytes, published_digest: str):
        self.zip_bytes, self.digest, self.zip_gets = zip_bytes, published_digest, 0

    def get(self, url, timeout=None):
        if url.endswith(".CHECKSUM"):
            return _FakeResponse(f"{self.digest}  file.zip\n".encode())
        self.zip_gets += 1
        return _FakeResponse(self.zip_bytes)


def test_download_verifies_and_is_idempotent(tmp_path):
    src = write_archive(synthetic_klines(periods=50), tmp_path / "src" / "x.zip")
    payload = src.read_bytes()
    session = _FakeSession(payload, hashlib.sha256(payload).hexdigest())
    dest = tmp_path / "raw"
    p1 = qd.download_month("BTCUSDT", "1m", 2024, 1, dest, session=session)
    p2 = qd.download_month("BTCUSDT", "1m", 2024, 1, dest, session=session)
    assert p1 == p2 and session.zip_gets == 1
    qd.verify_archive(p1)


def test_download_rejects_bad_checksum_and_leaves_nothing(tmp_path):
    session = _FakeSession(b"corrupted", "0" * 64)
    with pytest.raises(qd.ChecksumError):
        qd.download_month("BTCUSDT", "1m", 2024, 1, tmp_path, session=session)
    assert not any(tmp_path.glob("*.zip*"))


def test_month_range_and_urls():
    assert qd.month_range("2024-11", "2025-02") == [(2024, 11), (2024, 12), (2025, 1), (2025, 2)]
    assert qd.archive_url("BTCUSDT", "1m", 2025, 1).endswith(
        "/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2025-01.zip"
    )


def test_implausible_timestamps_fail_loudly(tmp_path):
    df = synthetic_klines(start="1970-01-20", periods=10)
    p = write_archive(df, tmp_path / "BTCUSDT-1m-2024-01.zip")
    with pytest.raises(qd.DataIntegrityError):
        qd.load_klines([p])
