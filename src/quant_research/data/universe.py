"""Symbol ↔ Chinese name, so a code can be recognised while it is being typed.

``600519.SH`` is unambiguous to a machine and unreadable to a person. Every
screen the tool produces is a list of codes, and every form it accepts asks for
one — which means the user is expected to already know that 300750 is 宁德时代
and that it trades on Shenzhen. That knowledge lives in the provider, not in the
tool: both adapters already fetch a ``code_name`` column and throw it away.

So this module keeps it. One table, ``data/universe/names.parquet``, refreshed on
demand rather than on every request — the universe query is slow (tens of seconds
on akshare) and names change about as often as companies rename themselves, which
is to say rarely.

Search is deliberately forgiving, because people type what they remember:

    600519      → prefix of the bare code
    600519.SH   → the same, with the exchange the user bothered to type
    茅台        → substring of the name
    宁德        → prefix of the name, ranked above a substring match

The ranking puts an exact code first, then code prefixes, then name prefixes,
then name substrings. Someone who types six digits wants that stock, not a list
of everything containing those digits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from quant_research.data.providers.base import MarketDataProvider, ProviderError, normalize_symbol

__all__ = [
    "Symbol",
    "load",
    "load_sectors",
    "refresh",
    "refresh_sectors",
    "save",
    "save_sectors",
    "search",
    "sectors_path",
    "table_path",
]

_COLUMNS = ["symbol", "name", "updated_at"]
_SECTOR_COLUMNS = ["symbol", "sector", "updated_at"]


class Symbol(NamedTuple):
    """One search hit: the code to submit, and the name to read."""

    symbol: str
    name: str


def table_path(directory: Path | str) -> Path:
    """Where the table lives under a data root."""
    return Path(directory) / "universe" / "names.parquet"


def save(names: dict[str, str], directory: Path | str) -> Path:
    """Write the whole table. An empty mapping is refused, not written.

    Overwriting a good table with nothing is the one failure worth blocking:
    it would silently degrade every autocomplete to bare codes until the next
    refresh, and nothing would look broken.
    """
    if not names:
        raise ValueError("refusing to save an empty name table")
    target = table_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    frame = pd.DataFrame(
        [(symbol, name, stamp) for symbol, name in sorted(names.items())],
        columns=_COLUMNS,
    )
    frame.to_parquet(target, index=False)
    return target


def load(directory: Path | str) -> pd.DataFrame:
    """The stored table, or an empty frame when it has never been refreshed."""
    target = table_path(directory)
    if not target.is_file():
        return pd.DataFrame(columns=_COLUMNS)
    frame = pd.read_parquet(target)
    missing = [column for column in _COLUMNS if column not in frame.columns]
    if missing:
        return pd.DataFrame(columns=_COLUMNS)
    return frame


def refresh(provider: MarketDataProvider, directory: Path | str) -> int:
    """Pull the universe from ``provider`` and persist it. Returns the row count."""
    try:
        names = provider.list_named_symbols()
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"刷新名称表失败：{type(exc).__name__}: {exc}") from exc
    save(names, directory)
    return len(names)


def _digits(text: str) -> str:
    return "".join(character for character in text if character.isdigit())


def search(frame: pd.DataFrame, query: str, limit: int = 20) -> list[Symbol]:
    """Rank matches of ``query`` against codes and names.

    An empty query returns nothing rather than the whole market: a dropdown that
    opens with five thousand rows is worse than one that waits for a keystroke.
    """
    text = (query or "").strip()
    if not text or frame is None or frame.empty:
        return []

    bare_query = _digits(text)[:6]
    upper = text.upper()
    wanted = _normalised_or_none(text)

    ranked: list[tuple[int, str, str, str]] = []
    for symbol, name in zip(frame["symbol"].astype(str), frame["name"]):
        label = "" if pd.isna(name) else str(name).strip()
        bare = symbol.partition(".")[0]
        if bare_query and bare.startswith(bare_query):
            # -1: the exact code, suffix and all — the user typed the whole thing
            rank = -1 if wanted == symbol else 0 if bare == bare_query else 1
            ranked.append((rank, bare, label, symbol))
        elif label and upper in label.upper():
            rank = 2 if label.upper().startswith(upper) else 3
            ranked.append((rank, bare, label, symbol))

    ranked.sort(key=lambda item: (item[0], item[1]))
    return [Symbol(symbol=item[3], name=item[2]) for item in ranked[:limit]]


def _normalised_or_none(text: str) -> str:
    try:
        return normalize_symbol(text)
    except ValueError:
        return ""


# --- 板块表 -------------------------------------------------------------------


def sectors_path(directory: Path | str) -> Path:
    """Where the sector table lives under a data root."""
    return Path(directory) / "universe" / "sectors.parquet"


def save_sectors(industries: dict[str, str], directory: Path | str) -> Path:
    """Write the whole sector table. An empty mapping is refused, not written.

    Same rule as the name table: overwriting a good table with nothing would
    silently strip every sector label and board aggregate until the next
    refresh, and nothing would look broken.
    """
    if not industries:
        raise ValueError("refusing to save an empty sector table")
    target = sectors_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    frame = pd.DataFrame(
        [(symbol, sector, stamp) for symbol, sector in sorted(industries.items())],
        columns=_SECTOR_COLUMNS,
    )
    frame.to_parquet(target, index=False)
    return target


def load_sectors(directory: Path | str) -> pd.DataFrame:
    """The stored sector table, or an empty frame when never refreshed."""
    target = sectors_path(directory)
    if not target.is_file():
        return pd.DataFrame(columns=_SECTOR_COLUMNS)
    frame = pd.read_parquet(target)
    missing = [column for column in _SECTOR_COLUMNS if column not in frame.columns]
    if missing:
        return pd.DataFrame(columns=_SECTOR_COLUMNS)
    return frame


def refresh_sectors(provider: MarketDataProvider, directory: Path | str) -> int:
    """Pull the sector table from ``provider`` and persist it. Returns rows."""
    try:
        industries = provider.list_industries()
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"刷新板块表失败：{type(exc).__name__}: {exc}") from exc
    if not industries:
        raise ProviderError(f"{provider.name} 没有发布行业数据；换个源，或手动维护板块表")
    save_sectors(industries, directory)
    return len(industries)
