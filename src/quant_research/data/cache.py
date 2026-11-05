"""A single-file cache of the whole market panel.

:func:`~quant_research.data.loader.load_price_panel` reads one file per symbol.
That is right for a handful of symbols and painful for a market: screening reads
the same panel over and over, and every pass re-reads every file.

This module keeps one long-format Parquet per ``(provider, adjust)`` pair and
appends one day at a time afterwards. Both halves of that key matter — forward
adjusted prices from two vendors are not the same numbers, and ranking across
them produces a ranking of nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from quant_research.data.loader import (
    DATE_COLUMN,
    PRICE_COLUMN,
    SYMBOL_COLUMN,
    DataMeta,
    PricePanel,
)

CACHE_DIRECTORY = "cache"
_INDEX = [DATE_COLUMN, SYMBOL_COLUMN]

__all__ = [
    "CACHE_DIRECTORY",
    "build_panel_cache",
    "cache_path",
    "load_panel_cache",
    "refresh_symbols",
    "to_long",
    "update_panel_cache",
    "write_panel_cache",
]


def cache_path(root: str | Path, provider: str, adjust: str) -> Path:
    """Where the panel for one ``(provider, adjust)`` pair lives."""
    return Path(root).expanduser() / CACHE_DIRECTORY / f"{provider}_{adjust}.parquet"


def to_long(panel: PricePanel) -> pd.DataFrame:
    """Flatten a panel to one row per ``(date, symbol)``, keeping every field.

    Long format is not aesthetics: a new day is appended rows, while a wide
    panel would have to be re-written column by column.
    """
    prices = panel.prices
    fields = {
        name: frame.reindex(index=prices.index, columns=prices.columns)
        for name, frame in ((PRICE_COLUMN, prices), *panel.extra.items())
    }
    index = pd.MultiIndex.from_product([prices.index, prices.columns], names=_INDEX)
    reshaped = {name: frame.to_numpy().reshape(-1) for name, frame in fields.items()}
    return pd.DataFrame(reshaped, index=index).reset_index()


def _to_wide(long: pd.DataFrame, column: str) -> pd.DataFrame:
    wide = long.pivot(index=DATE_COLUMN, columns=SYMBOL_COLUMN, values=column).sort_index()
    # pivot names the column axis after the values it pivoted on; downstream
    # compares this against panels that carry no axis name at all
    wide.columns.name = None
    wide.index.name = DATE_COLUMN
    return wide


def _write(long: pd.DataFrame, path: Path) -> Path:
    long = long.sort_values(_INDEX).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    long.to_parquet(path, index=False)
    return path


def write_panel_cache(panel: PricePanel, root: str | Path, provider: str, adjust: str) -> Path:
    """Replace the cached panel wholesale. Use :func:`update_panel_cache` to add a day."""
    return _write(to_long(panel), cache_path(root, provider, adjust))


def update_panel_cache(panel: PricePanel, root: str | Path, provider: str, adjust: str) -> Path:
    """Add the panel's rows to the cache, newest data winning.

    Appending one day should not mean rewriting the market, and running the
    update twice must leave the same cache behind — the nightly job will be run
    more than once against the same day whenever something needs a retry.
    """
    path = cache_path(root, provider, adjust)
    incoming = to_long(panel)
    if path.is_file():
        combined = pd.concat([pd.read_parquet(path), incoming], ignore_index=True)
        combined = combined.drop_duplicates(subset=_INDEX, keep="last")
    else:
        combined = incoming
    return _write(combined, path)


def load_panel_cache(
    root: str | Path,
    provider: str,
    adjust: str,
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> PricePanel:
    """Read the cached panel back as a :class:`PricePanel`.

    Missing prices are kept rather than rejected: a market always contains
    suspended names, and a screen that refuses to run because one stock is halted
    is not useful.
    """
    path = cache_path(root, provider, adjust)
    if not path.is_file():
        raise FileNotFoundError(
            f"no cached panel for {provider}/{adjust} at {path}; build it first"
        )

    long = pd.read_parquet(path)
    if symbols is not None:
        long = long[long[SYMBOL_COLUMN].isin(set(symbols))]
    if start is not None:
        long = long[long[DATE_COLUMN] >= pd.Timestamp(start)]
    if end is not None:
        long = long[long[DATE_COLUMN] <= pd.Timestamp(end)]
    if long.empty:
        raise ValueError(f"cached panel is empty after filtering: {path}")

    prices = _to_wide(long, PRICE_COLUMN)
    extra = {
        column: _to_wide(long, column)
        for column in long.columns
        if column not in _INDEX and column != PRICE_COLUMN
    }
    return PricePanel(
        prices=prices,
        meta=DataMeta(
            source=str(path),
            start=prices.index.min(),
            end=prices.index.max(),
            price_adjustment=adjust,
            frequency="daily",
            missing_policy="keep",
            symbols=tuple(str(column) for column in prices.columns),
        ),
        extra=extra,
    )


def _read_store(directory: Path, symbols: set[str] | None = None) -> pd.DataFrame:
    """Read per-symbol files into long format; ``symbols`` narrows the read."""
    paths = sorted(directory.glob("*.parquet")) + sorted(directory.glob("*.csv"))
    if symbols is not None:
        paths = [path for path in paths if path.stem in symbols]
    if not paths:
        raise ValueError(f"no per-symbol files found in {directory}")

    rows: list[pd.DataFrame] = []
    for path in paths:
        frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
        if frame.index.name == DATE_COLUMN or not isinstance(frame.index, pd.RangeIndex):
            frame = frame.reset_index()
        if DATE_COLUMN not in frame.columns:
            continue
        frame[DATE_COLUMN] = pd.to_datetime(frame[DATE_COLUMN])
        frame[SYMBOL_COLUMN] = path.stem
        rows.append(frame)

    if not rows:
        raise ValueError(f"no dated price files found in {directory}")
    return pd.concat(rows, ignore_index=True)


def build_panel_cache(
    root: str | Path,
    provider: str,
    adjust: str,
    store_root: str | Path | None = None,
) -> Path:
    """First build: read the store once, so the next thousand runs need not.

    Every field in the files is kept, including ``amount`` — liquidity filters
    need it, and the loader's optional-field list does not carry it.
    """
    base = Path(store_root).expanduser() if store_root is not None else Path(root).expanduser()
    directory = base / "raw" / provider / "daily"
    long = _read_store(directory)
    long = long.drop_duplicates(subset=_INDEX, keep="last")
    return _write(long, cache_path(root, provider, adjust))


def refresh_symbols(
    root: str | Path,
    provider: str,
    adjust: str,
    symbols: Sequence[str],
) -> Path:
    """Re-read some symbols' files into the cache, replacing only their rows.

    For the nightly run: a handful of files changed, and re-reading the whole
    market to pick up one day is not worth it.

    The files are read directly rather than through the panel loader because the
    loader drops ``amount`` — writing those NaNs back over the cache would
    quietly cost the liquidity filter its input.
    """
    wanted = set(symbols)
    if not wanted:
        return cache_path(root, provider, adjust)

    directory = Path(root).expanduser() / "raw" / provider / "daily"
    incoming = _read_store(directory, symbols=wanted)
    path = cache_path(root, provider, adjust)
    if not path.is_file():
        # no cache to merge into: writing only this run's symbols would shrink
        # the panel to them, and every other stored name would silently vanish
        # from screens and indicator boards
        return build_panel_cache(root, provider, adjust)
    kept = pd.read_parquet(path)
    kept = kept[~kept[SYMBOL_COLUMN].isin(wanted)]
    incoming = pd.concat([kept, incoming], ignore_index=True)
    return _write(incoming, path)
