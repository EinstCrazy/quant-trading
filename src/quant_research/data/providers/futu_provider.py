"""Bars and live quotes through Futu's OpenAPI.

What this provider is for, and — more importantly — what it is not:

- **Live quotes and order books**: this is where Futu is genuinely good. No fee
  per call, rate limits far looser than the scraping sources (snapshots allow 60
  requests per 30 seconds), and no account is needed to log in to OpenD.
- **Whole-market history**: do not. Futu meters historical K-lines by *distinct
  symbol within a rolling seven days* — 100 for an account holding under 10k HKD,
  300 at 10k, 1000 at 500k, 2000 at 5m. Covering ~5,000 A-share names is
  therefore a matter of weeks, not minutes. BaoStock stays the source for
  history; this provider exists for everything a nightly snapshot cannot give.

Three things to know before the first call:

- It needs **OpenD** running locally (default 127.0.0.1:11111). The SDK talks to
  that gateway, not to Futu directly. Futu also offers a REST API for callers who
  would rather not keep a daemon alive.
- **A-share LV1 quotes are free only for mainland-verified clients**, and the
  distinction is made on the IP OpenD logs in from. Run this offshore and
  A-shares are simply not served.
- Every symbol in a history call spends one unit of the rolling quota, and
  re-requesting the same symbol inside those seven days does not spend another.
  The collector's incremental tail re-read therefore costs one unit per symbol
  per week, which is exactly the bill worth watching.

The method shapes below follow Futu's official documentation (v10.12). They have
**not** been exercised against a live gateway from this repository — OpenD is a
user-side install, and there is no way to fake a market convincingly — so the
first run against a real gateway is the actual test.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, ClassVar

import pandas as pd

from quant_research.data.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderUnavailable,
    SourceUnreachable,
    normalise_bars,
    normalize_symbol,
)
from quant_research.logging_setup import get_logger

logger = get_logger("data.providers.futu")

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 11111

# Futu counts volume in shares; the store keeps lots, as every other provider does.
_SHARES_PER_LOT = 100

# A history request returns at most this many bars; longer ranges are paged.
_KLINE_PAGE = 1000
_MAX_PAGES = 20

# Snapshots take a list, but a list with no ceiling is how you find the ceiling.
_SNAPSHOT_BATCH = 100

# Project vocabulary -> Futu's AuType.
_ADJUST_MAP: dict[str, str] = {
    "qfq": "QFQ",
    "forward": "QFQ",
    "hfq": "HFQ",
    "backward": "HFQ",
    "none": "NONE",
    "": "NONE",
}

# Beijing listings are not mapped: Futu's A-share coverage here is Shanghai and
# Shenzhen, and pretending otherwise would produce codes it cannot serve.
_MARKETS: tuple[str, ...] = ("SH", "SZ", "HK", "US")

# K-line columns -> project columns. Futu calls turnover what we call amount.
_KLINE_COLUMNS: dict[str, str] = {
    "time_key": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "turnover": "amount",
}

# Snapshot columns -> project columns.
_SNAPSHOT_COLUMNS: dict[str, str] = {
    "code": "symbol",
    "last_price": "last_price",
    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "prev_close_price": "prev_close",
    "volume": "volume",
    "turnover": "amount",
    "turnover_rate": "turnover_rate",
}


def to_futu_code(symbol: str) -> str:
    """``600519.SH`` -> ``SH.600519``."""
    code, _, exchange = normalize_symbol(symbol).rpartition(".")
    return f"{exchange}.{code}"


def from_futu_code(code: str) -> str:
    """``SH.600519`` -> ``600519.SH``."""
    exchange, _, bare = str(code).partition(".")
    return normalize_symbol(f"{bare}.{exchange}")


class FutuProvider(MarketDataProvider):
    """Daily bars and live quotes for Shanghai, Shenzhen, Hong Kong and the US."""

    name: ClassVar[str] = "futu"

    def __init__(
        self,
        adjust: str = "qfq",
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        markets: Sequence[str] = ("SH", "SZ"),
    ) -> None:
        if adjust not in _ADJUST_MAP:
            raise ValueError(f"adjust must be one of {sorted(_ADJUST_MAP)}, got {adjust!r}")
        wanted = tuple(str(market).upper() for market in markets)
        unknown = [market for market in wanted if market not in _MARKETS]
        if unknown:
            raise ValueError(f"markets must be among {list(_MARKETS)}, got {unknown}")
        self.adjust = adjust
        self.host = host
        self.port = port
        self.markets = wanted

    @staticmethod
    def _futu() -> Any:
        try:
            import futu
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ProviderUnavailable(
                "futu-api is not installed. Futu also needs the OpenD gateway "
                "running and logged in:\n"
                "    pip install -e '.[futu]'\n"
                "    # start OpenD, log in with a 牛牛 / moomoo account"
            ) from exc
        return futu

    def _open(self) -> Any:
        """A quote context against the local gateway."""
        futu = self._futu()
        try:
            return futu.OpenQuoteContext(host=self.host, port=self.port)
        except Exception as exc:
            raise SourceUnreachable(
                f"could not reach OpenD at {self.host}:{self.port} — "
                f"is the gateway running and logged in? ({type(exc).__name__}: {exc})"
            ) from exc

    def list_symbols(self) -> list[str]:
        futu = self._futu()
        context = self._open()
        try:
            found: list[str] = []
            for market in self.markets:
                ret, data = context.get_stock_basicinfo(
                    market=getattr(futu.Market, market),
                    stock_type=futu.SecurityType.STOCK,
                )
                if ret != futu.RET_OK:
                    raise ProviderError(f"futu get_stock_basicinfo failed for {market}: {data}")
                for code in data["code"]:
                    try:
                        found.append(from_futu_code(code))
                    except ProviderError:
                        # A code this project cannot normalise is one it cannot store.
                        logger.debug("skipping unparseable futu code %s", code)
            return sorted(set(found))
        finally:
            context.close()

    def fetch_daily(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, pd.DataFrame]:
        """History for ``symbols``.

        One request per symbol, and one unit of the seven-day quota each. The
        collector asks for a short trailing window on every run, which is cheap
        in rows but not in quota — a nightly job over a thousand names will drain
        a thousand units a week.
        """
        if not symbols:
            return {}
        logger.info(
            "futu: requesting history for %d symbols; each spends one unit of the "
            "rolling 7-day quota",
            len(symbols),
        )
        futu = self._futu()
        autype = getattr(futu.AuType, _ADJUST_MAP[self.adjust])
        context = self._open()
        bars: dict[str, pd.DataFrame] = {}
        try:
            for symbol in symbols:
                code = to_futu_code(symbol)
                frame = self._history(
                    context,
                    futu,
                    code,
                    start.isoformat() if start else None,
                    end.isoformat() if end else None,
                    autype,
                )
                if frame is None or frame.empty:
                    continue
                bars[symbol] = self._bars(frame, symbol)
            return bars
        finally:
            context.close()

    def fetch_snapshot(self, symbols: Sequence[str]) -> pd.DataFrame:
        """Live quotes for ``symbols``, one row each.

        This is the reason to use Futu at all: snapshots are metered by rate, not
        by a weekly symbol budget, so watching the whole candidate list is
        affordable in a way that re-pulling their history is not.
        """
        wanted = [to_futu_code(symbol) for symbol in symbols]
        if not wanted:
            return pd.DataFrame()

        context = self._open()
        try:
            frames: list[pd.DataFrame] = []
            for offset in range(0, len(wanted), _SNAPSHOT_BATCH):
                batch = wanted[offset : offset + _SNAPSHOT_BATCH]
                futu = self._futu()
                ret, data = context.get_market_snapshot(batch)
                if ret != futu.RET_OK:
                    raise ProviderError(f"futu get_market_snapshot failed: {data}")
                if data is None or data.empty:
                    continue
                frames.append(self._quotes(data))
            if not frames:
                return pd.DataFrame()
            combined = pd.concat(frames, ignore_index=True)
            return combined.set_index("symbol") if "symbol" in combined else combined
        finally:
            context.close()

    # --- internals ---------------------------------------------------------

    def _history(
        self,
        context: Any,
        futu: Any,
        code: str,
        start: str | None,
        end: str | None,
        autype: Any,
    ) -> pd.DataFrame | None:
        """Page through the K-lines; a long range is more than one response."""
        frames: list[pd.DataFrame] = []
        key: str | None = None
        for _ in range(_MAX_PAGES):
            ret, data, key = context.request_history_kline(
                code,
                start=start,
                end=end,
                ktype=futu.KLType.K_DAY,
                autype=autype,
                max_count=_KLINE_PAGE,
                page_req_key=key,
            )
            if ret != futu.RET_OK:
                raise ProviderError(f"futu request_history_kline failed for {code}: {data}")
            if data is None or data.empty:
                break
            frames.append(data)
            if not key:
                break
        if not frames:
            return None
        return pd.concat(frames, ignore_index=True)

    def _bars(self, frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Rename to the project's vocabulary and convert shares to lots."""
        renamed = frame.rename(columns=_KLINE_COLUMNS)
        if "date" not in renamed.columns:
            raise ProviderError(f"futu returned K-lines for {symbol} without a time_key column")
        bars = renamed.set_index("date")
        bars.index = pd.to_datetime(bars.index)
        if "volume" in bars.columns:
            bars["volume"] = pd.to_numeric(bars["volume"], errors="coerce") / _SHARES_PER_LOT
        return normalise_bars(bars, source=f"futu:{symbol}")

    def _quotes(self, frame: pd.DataFrame) -> pd.DataFrame:
        renamed = frame.rename(columns=_SNAPSHOT_COLUMNS)
        keep = [column for column in _SNAPSHOT_COLUMNS.values() if column in renamed.columns]
        quotes = renamed.loc[:, keep].copy()
        if "symbol" in quotes.columns:
            quotes["symbol"] = [from_futu_code(code) for code in quotes["symbol"]]
        if "volume" in quotes.columns:
            quotes["volume"] = pd.to_numeric(quotes["volume"], errors="coerce") / _SHARES_PER_LOT
        return quotes


__all__ = ["FutuProvider", "from_futu_code", "to_futu_code"]
