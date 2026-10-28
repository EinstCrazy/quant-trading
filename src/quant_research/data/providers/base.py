"""Market data provider contract.

A provider knows exactly one thing: how to talk to a source and return *standard*
daily bars. Storage, incremental windows, retries, scheduling and logging all live
outside, so adding a new source never touches the collector.

Standard bar frame:

- index: ``DatetimeIndex`` (normalised to midnight, sorted, unique)
- columns: ``open``, ``high``, ``low``, ``close``, ``volume`` (required),
  ``amount``, ``pct_change`` (optional)
- all columns ``float64``; ``volume`` keeps the source unit (A-share sources report
  ``手``) and ``amount`` is in currency. The collector records the unit in its state
  file rather than converting silently — STAR-board lots are not 100 shares.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import ClassVar

import pandas as pd

REQUIRED_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
OPTIONAL_COLUMNS: tuple[str, ...] = ("amount", "pct_change")
STANDARD_COLUMNS: tuple[str, ...] = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


class ProviderError(RuntimeError):
    """The source could not be reached or returned unusable data."""


class ProviderUnavailable(ProviderError):
    """A third-party package required by the provider is not installed."""


class SourceUnreachable(ProviderError):
    """The source hung up instead of answering.

    A rate limit and a block look exactly like this: no HTTP status, no body, just
    a closed socket. It is named separately from a malformed response because the
    two ask for opposite reactions — hammering a source that is refusing us only
    deepens the throttle, whereas a bad response deserves an immediate retry.
    """


# Exception names meaning "transport refused or dropped us". Named rather than
# imported because the underlying clients (requests, urllib3) are optional.
_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "RemoteDisconnected",
        "ConnectionResetError",
        "BrokenPipeError",
        "ProtocolError",
        "ProtocolErrorWrapper",
        "SSLError",
    }
)


def is_transport_error(exc: BaseException) -> bool:
    """Whether a failure is "they hung up" rather than "the answer was junk"."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    names = {klass.__name__ for klass in type(exc).__mro__}
    return not names.isdisjoint(_TRANSPORT_ERROR_NAMES)


def explain(exc: BaseException, source: str) -> str:
    """Say what a refused connection actually means, and what to do about it."""
    return (
        f"{source} 取不到数据：服务端直接断开，没有返回任何响应。\n"
        f"这通常意味着被限流或拒绝（HTTP 状态码都没有，更不是数据问题）。\n"
        f"原始错误：{type(exc).__name__}: {exc}\n"
        f"怎么办：稍后再试；或换 baostock 采集 —— quant-collect --provider baostock "
        f"--symbols 600519.SH。连续重试会加重限流。"
    )


# First digit(s) -> exchange. Covers A shares, STAR/ChiNext and Beijing listings.
_EXCHANGE_BY_PREFIX: dict[str, str] = {
    "60": "SH",
    "68": "SH",
    "90": "SH",
    "50": "SH",
    "51": "SH",
    "58": "SH",
    "00": "SZ",
    "30": "SZ",
    "20": "SZ",
    "15": "SZ",
    "16": "SZ",
    "18": "SZ",
    "43": "BJ",
    "83": "BJ",
    "87": "BJ",
    "88": "BJ",
    "92": "BJ",
}
_SUFFIX_EXCHANGE: dict[str, str] = {
    ".SH": "SH",
    ".SS": "SH",
    ".XSHG": "SH",
    ".SZ": "SZ",
    ".XSHE": "SZ",
    ".BJ": "BJ",
}


def to_bare_symbol(symbol: str) -> str:
    """Return the six-digit code without exchange decoration (``600519.SH`` → ``600519``)."""
    digits = "".join(character for character in symbol if character.isdigit())
    if len(digits) != 6:
        raise ValueError(f"cannot parse a six-digit A-share code from {symbol!r}")
    return digits


def normalize_symbol(raw: str) -> str:
    """Return ``<code>.<exchange>``, inferring the exchange when it is omitted.

    Accepts ``600519``, ``600519.SH``, ``sh600519``, ``SH.600519`` and the same
    shapes for Shenzhen and Beijing listings. Suffixes win over prefix inference,
    so ``000001.SH`` stays on the Shanghai exchange (the index case).
    """
    text = raw.strip().upper().replace("_", ".")
    digits = to_bare_symbol(text)

    for suffix, exchange in _SUFFIX_EXCHANGE.items():
        if text.endswith(suffix):
            return f"{digits}.{exchange}"

    for prefix in (text[:2], text[:1]):
        if prefix in ("SH", "SZ", "BJ"):
            return f"{digits}.{prefix}"

    for length in (2, 1):
        inferred = _EXCHANGE_BY_PREFIX.get(digits[:length])
        if inferred is not None:
            return f"{digits}.{inferred}"

    raise ValueError(f"cannot infer the exchange for {raw!r}; pass it explicitly, e.g. {digits}.SH")


def normalise_bars(frame: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """Validate and standardise a provider frame, raising ``ProviderError`` on garbage."""
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ProviderError(f"{source} returned bars without required column(s) {missing}")

    available = [column for column in STANDARD_COLUMNS if column in frame.columns]
    bars = frame.loc[:, available].astype("float64")
    index = pd.DatetimeIndex(pd.to_datetime(bars.index))
    if index.isna().any():
        raise ProviderError(f"{source} returned rows without a parsable date")

    bars.index = index.normalize()
    bars = bars[~bars.index.duplicated(keep="last")].sort_index()
    bars = bars.dropna(subset=["close"])
    if bars.empty:
        return bars
    if (bars["close"] <= 0).any():
        raise ProviderError(f"{source} returned non-positive closes")
    bars.index.name = "date"
    return bars


class MarketDataProvider(ABC):
    """Source adapter returning standard daily bars."""

    name: ClassVar[str]

    @abstractmethod
    def list_symbols(self) -> list[str]:
        """Return every symbol the source can serve, normalised."""

    @abstractmethod
    def fetch_daily(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Return ``{symbol: bars}``; raise ``ProviderError`` when the source fails.

        Symbols that legitimately have no data in the window are omitted from the
        mapping rather than returned as empty frames, so the caller can tell
        "no trading yet" apart from "request failed".
        """


__all__ = [
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "STANDARD_COLUMNS",
    "MarketDataProvider",
    "ProviderError",
    "ProviderUnavailable",
    "SourceUnreachable",
    "explain",
    "is_transport_error",
    "normalise_bars",
    "normalize_symbol",
    "to_bare_symbol",
]
