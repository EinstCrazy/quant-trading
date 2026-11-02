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

from quant_research.indicators import boll, macd, rsi

__all__ = [
    "boll_position",
    "combine",
    "factor_correlation",
    "inverse_volatility",
    "liquidity",
    "ma_distance",
    "macd_trend",
    "momentum",
    "rsi_reversal",
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


# --- factors built on the technical indicators ------------------------------
#
# These are opt-in, not part of the default score. Three indicators of the same
# family do not add three views of the market; they add one view three times.
# More importantly, ``rsi_reversal`` points the opposite way to ``momentum``:
# switched on together, the score becomes whichever weight happens to be larger.


def rsi_reversal(close: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    """Low RSI scores high — a mean-reversion bet, deliberately the inverse of trend.

    Use it *or* ``momentum``, not both, unless the point is to hedge one against
    the other on purpose.
    """
    if window < 1:
        raise ValueError("window must be positive")
    return 50.0 - rsi(close, n=window)


def macd_trend(
    close: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """The MACD histogram, divided by price.

    The histogram is quoted in yuan: on a 5-yuan name and a 500-yuan name the
    same reading means completely different things, and ranking on the raw value
    would simply favour expensive stocks. Dividing by the close makes it a
    percentage of price, which is what the reader assumed it was.
    """
    if not 0 < fast < slow:
        raise ValueError("expected 0 < fast < slow")
    if signal < 1:
        raise ValueError("signal must be positive")
    return macd(close, fast=fast, slow=slow, signal=signal).hist / close


def boll_position(close: pd.DataFrame, window: int = 20, width: float = 2.0) -> pd.DataFrame:
    """Where the close sits inside its own band: 0 at the lower rail, 1 at the upper.

    Already a ratio, so names at different price levels are comparable without
    further scaling — the opposite situation to :func:`macd_trend`.
    """
    if window < 2:
        raise ValueError("window must be at least 2")
    if width <= 0:
        raise ValueError("width must be positive")
    bands = boll(close, n=window, k=width)
    span = bands.upper - bands.lower
    # a window with no movement collapses the band; there is no position inside
    # a band of zero width, and 0.5 would be an invented answer
    return (close - bands.lower) / span.where(span > 0)


def zscore_by_day(frame: pd.DataFrame) -> pd.DataFrame:
    """Standardise across symbols, one day at a time.

    Standardising the whole sample at once would make Monday's score depend on
    how the rest of the year turned out. The cross-section a decision can see is
    the one printed on that day's tape, nothing more.
    """
    spread = frame.std(axis=1)
    return frame.sub(frame.mean(axis=1), axis=0).div(spread.where(spread > 0), axis=0)


def factor_correlation(
    factors: Mapping[str, pd.DataFrame], method: str = "spearman"
) -> pd.DataFrame:
    """Average daily cross-sectional correlation between factors.

    This exists because "add another indicator" is the easiest way to make a
    score look better informed without making it better. Two factors at 0.95 are
    one judgement counted twice, and a weighted sum cannot tell the difference.

    Measured per day, then averaged: correlating the pooled panel instead would
    mix cross-sectional structure with the market's own drift over time.
    """
    names = list(factors)
    if len(names) < 2:
        raise ValueError("correlation needs at least two factors")

    index: pd.Index | None = None
    for frame in factors.values():
        index = frame.index if index is None else index.intersection(frame.index)
    assert index is not None

    total = pd.DataFrame(0.0, index=names, columns=names)
    counted = 0
    for day in index:
        row = pd.DataFrame({name: factors[name].loc[day] for name in names}).dropna()
        # a correlation over two names is ±1 by construction, which says nothing
        if len(row) < 3:
            continue
        # Spearman is Pearson on ranks; doing it explicitly keeps scipy out of
        # the dependency list for what is one method call either way
        matrix = row.rank().corr() if method == "spearman" else row.corr(method=method)
        total += matrix.fillna(0.0)
        counted += 1

    if not counted:
        raise ValueError("no day had three names with values in every factor")
    return total / counted


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
