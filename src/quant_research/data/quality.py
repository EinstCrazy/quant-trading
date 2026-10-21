"""Data quality diagnostics for price panels.

The first version of this module only *reports* problems; it never silently
repairs data. Fixing strategies (forward-fill limits, suspension handling,
calendar alignment) stay an explicit decision recorded in the experiment config.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from quant_research.data.loader import PricePanel

Jump = tuple[pd.Timestamp, str, float]


@dataclass(frozen=True)
class QualityReport:
    """Structured outcome of :func:`describe_panel`."""

    start: pd.Timestamp | None
    end: pd.Timestamp | None
    n_obs: int
    symbols: tuple[str, ...]
    missing_rate: pd.Series
    non_positive: pd.Series
    jumps: list[Jump]
    monotonic_dates: bool

    @property
    def is_clean(self) -> bool:
        return (
            self.monotonic_dates
            and not self.jumps
            and float(self.missing_rate.max() if len(self.missing_rate) else 0.0) == 0.0
            and int(self.non_positive.sum()) == 0
        )

    def to_frame(self) -> pd.DataFrame:
        """Return one row per symbol with the per-symbol diagnostics."""
        return pd.DataFrame(
            {
                "missing_rate": self.missing_rate,
                "non_positive": self.non_positive.astype("int64"),
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "start": None if self.start is None else str(self.start.date()),
            "end": None if self.end is None else str(self.end.date()),
            "n_obs": self.n_obs,
            "symbols": list(self.symbols),
            "missing_rate": self.missing_rate.to_dict(),
            "non_positive": self.non_positive.astype("int64").to_dict(),
            "jumps": [(str(date), symbol, ret) for date, symbol, ret in self.jumps],
            "monotonic_dates": self.monotonic_dates,
            "is_clean": self.is_clean,
        }


def describe_panel(panel: PricePanel, jump_threshold: float = 0.5) -> QualityReport:
    """Summarise a price panel and flag suspicious points.

    ``jumps`` lists single-day returns whose absolute value exceeds
    ``jump_threshold`` — a common symptom of unadjusted prices or a stock split.
    """
    prices = panel.prices
    if prices.empty:
        return QualityReport(
            start=None,
            end=None,
            n_obs=0,
            symbols=(),
            missing_rate=pd.Series(dtype="float64"),
            non_positive=pd.Series(dtype="int64"),
            jumps=[],
            monotonic_dates=True,
        )

    returns = prices.pct_change()
    jumps: list[Jump] = []
    for date, row in returns.iterrows():
        for symbol, value in row.items():
            if pd.notna(value) and abs(value) > jump_threshold:
                jumps.append((date, str(symbol), float(value)))

    return QualityReport(
        start=prices.index.min(),
        end=prices.index.max(),
        n_obs=len(prices),
        symbols=tuple(str(column) for column in prices.columns),
        missing_rate=prices.isna().mean(),
        non_positive=(prices <= 0).sum(),
        jumps=jumps,
        monotonic_dates=bool(
            prices.index.is_monotonic_increasing and not prices.index.has_duplicates
        ),
    )


__all__ = ["QualityReport", "describe_panel"]
