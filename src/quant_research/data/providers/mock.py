"""Offline provider generating deterministic synthetic bars.

Its purpose is to make the collection pipeline runnable and testable without
network access or third-party data packages — the same role
``data/sample/sample_prices.csv`` plays for the backtest.
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import numpy as np
import pandas as pd

from quant_research.data.providers.base import MarketDataProvider, normalise_bars, normalize_symbol

DEFAULT_SYMBOLS: tuple[str, ...] = ("600000.SH", "000001.SZ", "600519.SH")
DEFAULT_START = date(2024, 1, 2)


class MockProvider(MarketDataProvider):
    """Deterministic geometric random walk on a business-day calendar."""

    name: ClassVar[str] = "mock"

    def __init__(
        self,
        symbols: tuple[str, ...] | None = None,
        seed: int = 7,
        adjust: str = "none",
    ) -> None:
        # ``adjust`` is accepted so every provider shares one constructor contract;
        # synthetic prices have no corporate actions to adjust for.
        self.adjust = adjust
        self._symbols = tuple(normalize_symbol(item) for item in (symbols or DEFAULT_SYMBOLS))
        self._seed = seed
        if not self._symbols:
            raise ValueError("MockProvider needs at least one symbol")

    def list_symbols(self) -> list[str]:
        return list(self._symbols)

    def fetch_daily(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, pd.DataFrame]:
        begin = pd.Timestamp(start or DEFAULT_START)
        finish = pd.Timestamp(end or date.today())
        if finish < begin:
            return {}

        index = pd.bdate_range(begin, finish)
        if index.empty:
            return {}

        frames: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            normalised = normalize_symbol(symbol)
            if normalised not in self._symbols:
                continue
            # Seed per symbol so a symbol's history does not shift when others are added.
            rng = np.random.default_rng(self._seed + sum(ord(ch) for ch in normalised))
            steps = rng.normal(0.0004, 0.012, size=len(index))
            close = 20.0 * np.exp(np.cumsum(steps))
            spread = np.abs(rng.normal(0.008, 0.003, size=len(index)))
            previous = np.concatenate(([close[0]], close[:-1]))

            frame = pd.DataFrame(
                {
                    "open": previous * (1 + rng.normal(0.0, 0.001, size=len(index))),
                    "high": np.maximum(previous, close) * (1 + spread),
                    "low": np.minimum(previous, close) * (1 - spread),
                    "close": close,
                    "volume": rng.integers(1_000_000, 20_000_000, size=len(index)).astype(float),
                },
                index=index,
            )
            frame["amount"] = frame["volume"] * frame["close"]
            frame["pct_change"] = frame["close"].pct_change() * 100
            frames[normalised] = normalise_bars(frame, source=self.name)
        return frames


__all__ = ["DEFAULT_SYMBOLS", "MockProvider"]
