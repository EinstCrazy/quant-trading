"""Shanghai listings as the exchange itself publishes them.

What the SSE website hands out for free is the *security list*: authoritative, no
token, no third party in between. What it does not hand out is price history.
Daily bars from the exchange are licensed through 上证所信息网络 (sseinfo.com)
under a data agreement, and there is no free per-symbol historical endpoint to
call — the query endpoints on the site serve market summaries, not bars.

So this provider answers ``list_symbols`` and declines ``fetch_daily`` rather than
pretending otherwise. That still earns its place: the universe comes from the
exchange, the prices come from baostock or akshare, and when a scraped symbol list
and the official one disagree, you know which one to believe.
"""

from __future__ import annotations

import urllib.request
from datetime import date
from typing import ClassVar

import pandas as pd

from quant_research.data.providers.base import (
    MarketDataProvider,
    ProviderError,
    normalize_symbol,
)

_LIST_URL = "http://query.sse.com.cn/security/stock/downloadStockListFile.do"
# 1 = main-board A shares, 8 = STAR Market. (2 is B shares, 3 is pre-disclosure.)
_STOCK_TYPES: tuple[str, ...] = ("1", "8")
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    # the endpoint refuses requests without an SSE referer
    "Referer": "https://www.sse.com.cn/",
}
_TIMEOUT = 20.0


class SseProvider(MarketDataProvider):
    """The Shanghai universe, straight from the exchange. No price history."""

    name: ClassVar[str] = "sse"

    @staticmethod
    def _download(stock_type: str) -> str:
        request = urllib.request.Request(f"{_LIST_URL}?stockType={stock_type}", headers=_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read()
        except OSError as exc:
            raise ProviderError(f"could not reach the SSE list endpoint: {exc}") from exc
        # the file is GBK-encoded, tab separated, with a header row
        return raw.decode("gbk", errors="replace")

    def list_symbols(self) -> list[str]:
        symbols: set[str] = set()
        for stock_type in _STOCK_TYPES:
            for line in self._download(stock_type).splitlines():
                cells = [cell.strip() for cell in line.split("\t")]
                if len(cells) < 3:
                    continue
                code = cells[2]  # 证券代码, not 公司代码
                if len(code) != 6 or not code.isdigit():
                    continue  # header row, or a blank line
                symbols.add(normalize_symbol(code))

        if not symbols:
            raise ProviderError("the SSE list endpoint returned no parseable symbol")
        return sorted(symbols)

    def fetch_daily(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Not supported, and not silently empty.

        Raising beats returning nothing: a quiet empty mapping would look exactly
        like "no trading in this window" to the collector, and the run would be
        recorded as a success that collected nothing.
        """
        raise ProviderError(
            "the SSE website publishes no free per-symbol price history. "
            "Use provider 'baostock' or 'akshare' for daily bars; 'sse' only "
            "supplies the authoritative security list."
        )


__all__ = ["SseProvider"]
