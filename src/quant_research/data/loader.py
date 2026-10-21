"""Loading and validation of local market data.

Two entry points are provided:

- :func:`load_close_prices` — single-symbol CSV, kept for backwards compatibility.
- :func:`load_price_panel` — the cross-layer contract: a ``date x symbol`` price
  panel plus :class:`DataMeta` describing where the data came from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from quant_research.logging_setup import get_logger

logger = get_logger("data")

DATE_COLUMN = "date"
SYMBOL_COLUMN = "symbol"
PRICE_COLUMN = "close"
OPTIONAL_FIELDS = ("open", "high", "low", "volume")


@dataclass(frozen=True)
class DataMeta:
    """Provenance of a price panel. Without it a result is not reproducible."""

    source: str = "unknown"
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    timezone: str = "UTC"
    price_adjustment: str = "adjusted"
    frequency: str = "daily"
    missing_policy: str = "raise"
    symbols: tuple[str, ...] = ()

    @property
    def n_symbols(self) -> int:
        return len(self.symbols)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "start": None if self.start is None else str(self.start.date()),
            "end": None if self.end is None else str(self.end.date()),
            "timezone": self.timezone,
            "price_adjustment": self.price_adjustment,
            "frequency": self.frequency,
            "missing_policy": self.missing_policy,
            "symbols": list(self.symbols),
        }


@dataclass(frozen=True)
class PricePanel:
    """A ``date x symbol`` price panel with provenance and optional extra fields."""

    prices: pd.DataFrame
    meta: DataMeta = field(default_factory=DataMeta)
    extra: Mapping[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(str(column) for column in self.prices.columns)

    def field(self, name: str) -> pd.DataFrame:
        """Return a field panel: ``close`` (the panel itself) or an extra field."""
        if name == PRICE_COLUMN:
            return self.prices
        if name not in self.extra:
            raise KeyError(
                f"Field {name!r} is not available on this panel. "
                f"Loaded fields: {[PRICE_COLUMN, *self.extra]}"
            )
        return self.extra[name]

    def has_field(self, name: str) -> bool:
        return name == PRICE_COLUMN or name in self.extra


def load_close_prices(
    path: str | Path,
    date_column: str = DATE_COLUMN,
    price_column: str = PRICE_COLUMN,
) -> pd.Series:
    """Load a CSV containing dates and closing prices into a sorted Series."""
    frame = pd.read_csv(path, parse_dates=[date_column])
    if date_column not in frame or price_column not in frame:
        raise ValueError(f"CSV must contain {date_column!r} and {price_column!r} columns")

    prices = frame.set_index(date_column)[price_column].sort_index()
    if prices.index.has_duplicates:
        raise ValueError("Date column contains duplicate values")
    if prices.isna().any():
        raise ValueError("Closing prices contain missing values")
    if (prices <= 0).any():
        raise ValueError("Closing prices must be positive")

    prices.name = price_column
    return prices


def _read_csv(path: Path, date_column: str, check_duplicates: bool = True) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=[date_column])
    if date_column not in frame.columns:
        raise ValueError(f"CSV {path} must contain a {date_column!r} column")
    frame = frame.set_index(date_column).sort_index()
    if check_duplicates and frame.index.has_duplicates:
        raise ValueError(f"CSV {path} contains duplicate dates")
    return frame


def _collect_symbol_frames(
    path: Path,
    date_column: str,
    symbol_column: str,
    fallback_symbol: str | None,
) -> dict[str, pd.DataFrame]:
    """Return ``{symbol: DataFrame}`` for either a directory or a single file."""
    if path.is_dir():
        files = sorted(path.glob("*.csv"))
        if not files:
            raise FileNotFoundError(f"No CSV files found in directory: {path}")
        return {file.stem: _read_csv(file, date_column) for file in files}

    frame = _read_csv(path, date_column, check_duplicates=False)
    if symbol_column in frame.columns:
        frames = {
            str(symbol): group.drop(columns=[symbol_column])
            for symbol, group in frame.groupby(symbol_column, sort=True)
        }
        duplicated = [symbol for symbol, group in frames.items() if group.index.has_duplicates]
        if duplicated:
            raise ValueError(f"CSV {path} contains duplicate dates for: {sorted(duplicated)}")
        return frames

    if frame.index.has_duplicates:
        raise ValueError(f"CSV {path} contains duplicate dates")

    symbol = fallback_symbol or path.stem
    return {symbol: frame}


def _stack(frames: Mapping[str, pd.DataFrame], column: str) -> pd.DataFrame:
    series = {
        symbol: frame[column].rename(symbol)
        for symbol, frame in frames.items()
        if column in frame.columns
    }
    return pd.concat(series, axis=1) if series else pd.DataFrame()


def _default_settings() -> tuple[str, str, Path]:
    """Return ``(timezone, price_adjustment, data_directory)`` from settings.yaml."""
    from quant_research.config import load_settings

    try:
        settings = load_settings()
    except (FileNotFoundError, ValueError):
        return "UTC", "adjusted", Path("data")
    return settings.data.timezone, settings.data.price_adjustment, settings.data.directory


def load_price_panel(
    path: str | Path | None = None,
    *,
    symbols: Sequence[str] | None = None,
    date_column: str = DATE_COLUMN,
    symbol_column: str = SYMBOL_COLUMN,
    price_column: str = PRICE_COLUMN,
    start: str | None = None,
    end: str | None = None,
    missing_policy: str = "raise",
    meta: DataMeta | None = None,
) -> PricePanel:
    """Load CSV price data into a ``date x symbol`` panel with provenance.

    Accepts either a directory of per-symbol CSVs (``<SYMBOL>.csv``) or a single
    CSV that optionally carries a ``symbol`` column. Extra columns such as
    ``open``, ``high``, ``low`` and ``volume`` are preserved in ``PricePanel.extra``.

    ``missing_policy="raise"`` (default) rejects missing prices outright, matching
    the conservative behaviour of :func:`load_close_prices`.
    """
    if missing_policy not in ("raise", "keep"):
        raise ValueError("missing_policy must be either 'raise' or 'keep'")

    timezone, price_adjustment, data_directory = _default_settings()
    if path is None:
        path = data_directory / "raw"
    path = Path(path).expanduser()

    frames = _collect_symbol_frames(
        path, date_column, symbol_column, symbols[0] if symbols else None
    )
    prices = _stack(frames, price_column)
    if prices.empty:
        raise ValueError(f"No {price_column!r} column found in: {path}")

    prices = prices.sort_index()
    if prices.index.has_duplicates:
        raise ValueError("Panel contains duplicate dates")

    if symbols is not None:
        missing = sorted(set(symbols) - set(prices.columns))
        if missing:
            raise ValueError(f"Requested symbols not found in {path}: {missing}")
        prices = prices[list(symbols)]

    if start is not None:
        prices = prices.loc[prices.index >= pd.Timestamp(start)]
    if end is not None:
        prices = prices.loc[prices.index <= pd.Timestamp(end)]
    if prices.empty:
        raise ValueError(f"No data left after applying the requested date range: {path}")

    if (prices <= 0).any().any():
        raise ValueError("Closing prices must be positive")
    if missing_policy == "raise" and prices.isna().any().any():
        raise ValueError("Closing prices contain missing values")

    extra = {
        column: _stack(frames, column).reindex(index=prices.index, columns=prices.columns)
        for column in OPTIONAL_FIELDS
        if any(column in frame.columns for frame in frames.values())
    }

    panel_meta = meta or DataMeta(
        source=str(path),
        start=prices.index.min(),
        end=prices.index.max(),
        timezone=timezone,
        price_adjustment=price_adjustment,
        frequency="daily",
        missing_policy=missing_policy,
        symbols=tuple(str(column) for column in prices.columns),
    )

    logger.info(
        "loaded panel from %s: %d rows x %d symbols (%s ~ %s)",
        path,
        len(prices),
        prices.shape[1],
        prices.index.min().date(),
        prices.index.max().date(),
    )
    return PricePanel(prices=prices, meta=panel_meta, extra=extra)


__all__ = ["DataMeta", "PricePanel", "load_close_prices", "load_price_panel"]
