"""Write synthetic archives for CI smoke tests of the notebook. Not used for results."""

from __future__ import annotations

import argparse
from pathlib import Path

from qsr.synthetic import synthetic_klines, write_monthly_archives

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dest", default="data/synthetic")
    p.add_argument("--start", default="2024-12-01")
    p.add_argument("--days", type=int, default=62)
    a = p.parse_args()
    df = synthetic_klines(start=a.start, periods=a.days * 1440, seed=0, signal=0.2)
    for path in write_monthly_archives(df, Path(a.dest), us_from="2025-01"):
        print(path)
