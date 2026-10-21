"""Cash and position bookkeeping for the share-based accounting mode."""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.typing import NDArray


class Ledger:
    """Cash plus a share vector, valued against close prices."""

    def __init__(self, symbols: tuple[str, ...], cash: float) -> None:
        self.symbols = symbols
        self.cash = float(cash)
        self.shares: NDArray[np.float64] = np.zeros(len(symbols), dtype="float64")

    def market_value(self, prices: NDArray[np.float64]) -> float:
        """Value holdings at ``prices``; symbols without a price contribute zero."""
        return float(np.nansum(self.shares * prices))

    def equity(self, prices: NDArray[np.float64]) -> float:
        return self.cash + self.market_value(prices)

    def weights(self, prices: NDArray[np.float64]) -> NDArray[np.float64]:
        total = self.equity(prices)
        if total <= 0:
            return np.zeros(len(self.symbols), dtype="float64")
        return (self.shares * prices) / total

    def positions(self) -> pd.Series:
        return pd.Series(self.shares, index=list(self.symbols), name="shares")


__all__ = ["Ledger"]
