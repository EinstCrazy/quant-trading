"""Trading-day awareness without a hard dependency on a calendar service.

Three levels of knowledge, in increasing order of accuracy:

1. nothing loaded — fall back to "Monday to Friday";
2. a cached date list — read from a local CSV, no network;
3. refresh from AKShare — opt-in, because the endpoint can stall for a long time.

The collector records which level it used, so a holiday loss is visible in the
report instead of being mistaken for a data problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from quant_research.logging_setup import get_logger

logger = get_logger("data.calendar")

_WEEKEND = (5, 6)


@dataclass(frozen=True)
class TradingCalendar:
    """Trading days, or a weekday heuristic when ``days`` is unknown."""

    days: frozenset[date] | None = None
    source: str = "weekday"

    @property
    def is_exact(self) -> bool:
        return self.days is not None

    def is_trading_day(self, day: date) -> bool:
        if self.days is not None:
            return day in self.days
        return day.weekday() not in _WEEKEND

    def previous_trading_day(self, day: date, max_lookback: int = 30) -> date:
        """Return ``day`` itself when it trades, otherwise the closest earlier day."""
        candidate = day
        for _ in range(max_lookback):
            if self.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        return day

    def next_trading_day(self, day: date, max_lookahead: int = 30) -> date:
        candidate = day
        for _ in range(max_lookahead):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        return day

    @classmethod
    def from_file(cls, path: str | Path) -> TradingCalendar:
        """Load a cached ``date`` column CSV (one trading day per row)."""
        frame = pd.read_csv(Path(path).expanduser(), parse_dates=["date"])
        if "date" not in frame.columns:
            raise ValueError(f"calendar file must contain a 'date' column: {path}")
        values = pd.DatetimeIndex(frame["date"]).date
        return cls(days=frozenset(values.tolist()), source=str(path))

    def save(self, path: str | Path) -> Path:
        """Persist the loaded days so later runs avoid the network."""
        if self.days is None:
            raise ValueError("cannot save a calendar that has no explicit days")
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame({"date": sorted(self.days)})
        frame.to_csv(target, index=False)
        return target

    @classmethod
    def load_or_weekday(cls, path: str | Path | None) -> TradingCalendar:
        """Use the cache when present; never raise because it is missing."""
        if path is None:
            return cls()
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            return cls()
        try:
            return cls.from_file(candidate)
        except (ValueError, OSError, pd.errors.ParserError) as exc:
            logger.warning("ignoring unreadable trading calendar %s: %s", candidate, exc)
            return cls()

    @classmethod
    def from_akshare(cls) -> TradingCalendar:
        """Fetch the official calendar. Slow and network-bound — call explicitly."""
        try:
            import akshare
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("akshare is required to refresh the trading calendar") from exc

        try:
            table = akshare.tool_trade_date_hist_sina()
        except Exception as exc:
            raise RuntimeError(f"failed to download the trading calendar: {exc}") from exc

        if table is None or table.empty:
            raise RuntimeError("trading calendar download returned no rows")
        values = pd.DatetimeIndex(table.iloc[:, 0]).date
        return cls(days=frozenset(values.tolist()), source="akshare")


__all__ = ["TradingCalendar"]
