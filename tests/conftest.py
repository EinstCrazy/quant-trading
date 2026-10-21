"""Shared fixtures for the test suite."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def hand_prices() -> pd.DataFrame:
    """A five-bar single-symbol panel small enough to check by hand."""
    return pd.DataFrame(
        {"ASSET": [100.0, 101.0, 103.0, 102.0, 104.0]},
        index=pd.bdate_range("2024-01-01", periods=5),
    )


@pytest.fixture
def sample_prices() -> pd.DataFrame:
    """A deterministic three-symbol panel (120 business days)."""
    rng = np.random.default_rng(7)
    index = pd.bdate_range("2023-01-02", periods=120, name="date")
    data = {
        symbol: 100.0 * np.cumprod(1 + rng.normal(0.0003, 0.011, len(index)))
        for symbol in ("ALPHA", "BETA", "GAMMA")
    }
    return pd.DataFrame(data, index=index)


@pytest.fixture
def flat_weights(sample_prices: pd.DataFrame) -> pd.DataFrame:
    """Always fully invested in a single symbol."""
    weights = pd.DataFrame(0.0, index=sample_prices.index, columns=sample_prices.columns)
    weights["ALPHA"] = 1.0
    return weights
