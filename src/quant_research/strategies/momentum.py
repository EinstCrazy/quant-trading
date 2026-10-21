"""Simple long-only moving-average momentum signals."""

from __future__ import annotations

import pandas as pd

from quant_research.strategies.base import register


@register("ma_cross")
def moving_average_signal(
    close: pd.Series | pd.DataFrame,
    fast_window: int = 20,
    slow_window: int = 50,
    min_periods: int | None = None,
) -> pd.Series | pd.DataFrame:
    """Return a long-only 0/1 signal from fast and slow moving averages.

    The signal is 1 when the fast average is above the slow average. Periods
    without a complete slow-window average remain flat (0). Works for a single
    ``Series`` or a ``DataFrame`` of symbols (computed column-wise).
    """
    if fast_window < 1 or slow_window < 1:
        raise ValueError("Window sizes must be positive")
    if fast_window >= slow_window:
        raise ValueError("fast_window must be less than slow_window")

    required = slow_window if min_periods is None else min_periods
    fast_average = close.rolling(window=fast_window, min_periods=min(fast_window, required)).mean()
    slow_average = close.rolling(window=slow_window, min_periods=required).mean()

    signal = (fast_average > slow_average).fillna(False).astype("int8")
    if isinstance(signal, pd.Series):
        return signal.rename("signal")
    return signal


__all__ = ["moving_average_signal"]
