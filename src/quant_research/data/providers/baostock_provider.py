"""A-share daily bars from BaoStock.

BaoStock is free, needs no token and its interface has barely moved in years —
roughly the opposite of the scraping sources: AKShare covers far more, BaoStock
breaks far less. Keeping both means a scrape endpoint changing upstream is an
inconvenience rather than an outage.

Two things a caller should not have to discover the hard way:

- **BaoStock counts volume in shares; AKShare counts lots (手).** Left alone, one
  store fed by both would hold numbers a hundredfold apart, and nothing in the
  schema would say so. This provider converts to lots, matching the unit the
  collector already records.
- **Suspended days are returned as flat bars** (open = high = low = close =
  previous close, ``tradestatus = 0``). They are dropped here rather than stored,
  so a halt does not look like a day on which nothing happened at the same price.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, ClassVar

import pandas as pd

from quant_research.data.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderUnavailable,
    normalise_bars,
    normalize_symbol,
)
from quant_research.logging_setup import get_logger

logger = get_logger("data.providers.baostock")

# BaoStock tags codes with the exchange: sh.600519 / sz.000001 / bj.430047.
_EXCHANGE_TAG: dict[str, str] = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

# Project vocabulary -> BaoStock's adjustflag (1 = backward, 2 = forward, 3 = none).
# 「J66货币金融服务」的前缀是分类编码，不是名字
_INDUSTRY_PREFIX = re.compile(r"^[A-Za-z]?\d+")

_ADJUST_MAP: dict[str, str] = {
    "qfq": "2",
    "forward": "2",
    "hfq": "1",
    "backward": "1",
    "none": "3",
    "": "3",
}

_FIELDS = "date,open,high,low,close,volume,amount,turn,pctChg,tradestatus"
_NUMERIC = ("open", "high", "low", "close", "volume", "amount", "turn", "pctChg")
_COLUMN_MAP = {"pctChg": "pct_change", "turn": "turnover"}
_SHARES_PER_LOT = 100  # BaoStock reports shares; the store keeps lots
_EARLIEST = "1990-01-01"


class BaostockProvider(MarketDataProvider):
    """Daily bars for Shanghai, Shenzhen and Beijing listings."""

    name: ClassVar[str] = "baostock"

    def __init__(self, adjust: str = "qfq") -> None:
        if adjust not in _ADJUST_MAP:
            raise ValueError(f"adjust must be one of {sorted(_ADJUST_MAP)}, got {adjust!r}")
        self.adjust = adjust

    @staticmethod
    def _baostock() -> Any:
        try:
            import baostock
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ProviderUnavailable(
                "baostock is not installed. Install the collection extras with:\n"
                "    pip install -e '.[collect]'"
            ) from exc
        return baostock

    def _connect(self) -> Any:
        """Log in and return the module; BaoStock needs a session for every query."""
        baostock = self._baostock()
        result = baostock.login()
        if getattr(result, "error_code", "0") != "0":
            raise ProviderError(f"baostock login failed: {result.error_msg}")
        return baostock

    @staticmethod
    def _code(normalised: str) -> str:
        code, _, exchange = normalised.partition(".")
        tag = _EXCHANGE_TAG.get(exchange)
        if tag is None:
            raise ProviderError(f"{normalised}: BaoStock does not cover this exchange")
        return f"{tag}.{code}"

    def _tradable_rows(self) -> list[list[str]]:
        """``[code, tradeStatus, code_name]`` for the latest day that answers.

        ``query_all_stock`` answers only for a trading day: asked on a weekend, or
        before the day's first update, it returns nothing at all. So walk back from
        today — at most a long weekend's worth — and take the first day that
        answers.
        """
        baostock = self._connect()
        rows: list[list[str]] = []
        try:
            for offset in range(7):
                day = (date.today() - timedelta(days=offset)).strftime("%Y-%m-%d")
                result = baostock.query_all_stock(day=day)
                if getattr(result, "error_code", "1") != "0":
                    raise ProviderError(f"query_all_stock failed: {result.error_msg}")
                while result.next():
                    rows.append(result.get_row_data())
                if rows:
                    break
        finally:
            baostock.logout()

        tradable = [row for row in rows if len(row) > 1 and row[1] == "1"]
        if not tradable:
            raise ProviderError("query_all_stock returned no tradable symbol")
        return tradable

    def list_symbols(self) -> list[str]:
        """Every tradable listing, as of the most recent trading day."""
        return [normalize_symbol(row[0]) for row in self._tradable_rows()]

    def list_named_symbols(self) -> dict[str, str]:
        """The same universe, keeping the ``code_name`` column it already carries."""
        return {
            normalize_symbol(row[0]): (row[2].strip() if len(row) > 2 else "")
            for row in self._tradable_rows()
        }

    def list_industries(self) -> dict[str, str]:
        """行业归属（``query_stock_industry``）：``{symbol: 行业名}``。

        行结构是 ``[updateDate, code, code_name, industry, classification]``——
        代码在第 2 列、行业在第 4 列，不是最先想到的那两列。行业名带着分类
        前缀（``J66货币金融服务``），前缀是编码不是名字，去掉。
        同一只票可能同时出现证监会与申万两行，后者覆盖前者（申万更细）。
        个别代码归一化失败就跳过那一行——一条脏行不该抬走整张板块表。
        """
        baostock = self._connect()
        found: dict[str, str] = {}
        try:
            result = baostock.query_stock_industry()
            if getattr(result, "error_code", "1") != "0":
                raise ProviderError(f"query_stock_industry failed: {result.error_msg}")
            while result.next():
                row = result.get_row_data()
                if len(row) < 4:
                    continue
                industry = _INDUSTRY_PREFIX.sub("", row[3].strip()).strip() or row[3].strip()
                if not industry:
                    continue
                try:
                    found[normalize_symbol(row[1])] = industry
                except ValueError:
                    logger.debug("skipping unnormalisable industry row %s", row[1])
        finally:
            baostock.logout()
        return found

    def fetch_daily(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, pd.DataFrame]:
        baostock = self._connect()
        start_text = pd.Timestamp(start).strftime("%Y-%m-%d") if start else _EARLIEST
        end_text = pd.Timestamp(end or date.today()).strftime("%Y-%m-%d")
        adjust = _ADJUST_MAP[self.adjust]

        frames: dict[str, pd.DataFrame] = {}
        try:
            for symbol in symbols:
                normalised = normalize_symbol(symbol)
                try:
                    result = baostock.query_history_k_data_plus(
                        code=self._code(normalised),
                        fields=_FIELDS,
                        start_date=start_text,
                        end_date=end_text,  # inclusive on both ends
                        frequency="d",
                        adjustflag=adjust,
                    )
                    if getattr(result, "error_code", "0") != "0":
                        raise ProviderError(f"{normalised}: {result.error_msg}")
                    rows: list[list[str]] = []
                    while result.next():
                        rows.append(result.get_row_data())
                except ProviderError:
                    raise
                except Exception as exc:
                    raise ProviderError(f"{normalised}: {type(exc).__name__}: {exc}") from exc

                if not rows:
                    continue  # no trading history in the window

                frame = pd.DataFrame(rows, columns=_FIELDS.split(","))
                frame = frame[frame["tradestatus"] == "1"]  # drop suspended days
                if frame.empty:
                    continue

                for column in _NUMERIC:
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
                frame["volume"] = frame["volume"] / _SHARES_PER_LOT

                frame = frame.rename(columns=_COLUMN_MAP).set_index("date")
                frames[normalised] = normalise_bars(frame, source=f"{self.name}:{normalised}")
        finally:
            baostock.logout()
        return frames


__all__ = ["BaostockProvider"]
