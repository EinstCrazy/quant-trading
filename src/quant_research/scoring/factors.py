"""Cross-sectional factors.

Every function here maps a ``date x symbol`` field panel to a same-shaped score
panel, and every value may only look backwards: the score for day T uses prices
up to and including day T, never later. That is what makes a nightly run honest
— the score for a given day is the same number whether it was computed that
night or years later with the whole history in hand.
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

__all__ = [
    "combine",
    "inverse_volatility",
    "liquidity",
    "ma_distance",
    "momentum",
    "zscore_by_day",
]


def momentum(close: pd.DataFrame, window: int = 60, skip_recent: int = 5) -> pd.DataFrame:
    """Return over the last ``window`` days, ignoring the newest ``skip_recent``.

    The newest days are left out on purpose: the last few sessions are the ones
    most likely to mean-reverse, so including them measures yesterday's noise
    rather than the trend.
    """
    if window <= 1:
        raise ValueError("window must be greater than 1")
    if not 0 <= skip_recent < window:
        raise ValueError("skip_recent must be in [0, window)")
    return close.shift(skip_recent) / close.shift(window) - 1.0


def ma_distance(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """How far the close sits above its own moving average, in percent."""
    if window < 1:
        raise ValueError("window must be positive")
    average = close.rolling(window, min_periods=window).mean()
    return close / average - 1.0


def inverse_volatility(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Reciprocal of recent volatility — quiet names score higher.

    A zero deviation would divide by zero, and a flat price is not a low-risk
    opportunity but a missing one, so those stay undefined.
    """
    if window < 2:
        raise ValueError("window must be at least 2")
    risk = close.pct_change().rolling(window, min_periods=window).std()
    return 1.0 / risk.where(risk > 0)


def liquidity(amount: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Average turnover; used as a filter rather than as a score."""
    if window < 1:
        raise ValueError("window must be positive")
    return amount.rolling(window, min_periods=window).mean()


def zscore_by_day(frame: pd.DataFrame) -> pd.DataFrame:
    """Standardise across symbols, one day at a time.

    Standardising the whole sample at once would make Monday's score depend on
    how the rest of the year turned out. The cross-section a decision can see is
    the one printed on that day's tape, nothing more.
    """
    spread = frame.std(axis=1)
    return frame.sub(frame.mean(axis=1), axis=0).div(spread.where(spread > 0), axis=0)


def combine(
    factors: Mapping[str, pd.DataFrame],
    weights: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    """Weighted sum of per-day standardised factors.

    Unweighted by default. Scores are aligned by intersection, so a factor that
    has not warmed up yet on some names lowers nothing — those names simply
    carry fewer factors, which the caller can see in the per-factor columns.
    """
    if not factors:
        raise ValueError("combine needs at least one factor")

    names = list(factors)
    shares = {name: float(weights.get(name, 1.0)) for name in names} if weights else None
    standardised = {name: zscore_by_day(frame) for name, frame in factors.items()}

    total: pd.DataFrame | None = None
    for name, frame in standardised.items():
        share = 1.0 if shares is None else shares[name]
        contribution = frame * share
        total = contribution if total is None else total.add(contribution, fill_value=0.0)
    assert total is not None
    return total
