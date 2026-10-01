"""Shared fixtures. Test data comes from qsr.synthetic (never real market data)."""

from __future__ import annotations

import pandas as pd
import pytest

from qsr.synthetic import synthetic_klines


@pytest.fixture
def klines() -> pd.DataFrame:
    return synthetic_klines(periods=6_000, seed=1)
