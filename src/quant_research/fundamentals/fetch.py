"""Quarterly reports from BaoStock, one row per symbol per quarter.

Four tables — profit, balance, cash flow, growth — come back separately and are
keyed by ``(code, year, quarter)``, so a single quarter costs four queries. They
are merged into one wide row here, because every downstream question ("is this
company leveraged?") needs fields from more than one of them.

``pubDate`` is kept. Nothing else in this module can tell you whether a report
existed on a given day, and a screening that uses a report before it was
published is not a screening — it is a look into the future with extra steps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.data.providers.base import ProviderError, ProviderUnavailable, normalize_symbol

__all__ = [
    "ReportPanel",
    "collect",
    "load",
    "quarters_back",
    "store",
]

# table name -> (baostock query method, fields worth keeping)
_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "profit": (
        "query_profit_data",
        ("roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM", "MBRevenue"),
    ),
    "balance": (
        "query_balance_data",
        (
            "currentRatio",
            "quickRatio",
            "cashRatio",
            "liabilityToAsset",
            "assetToEquity",
            "YOYLiability",
        ),
    ),
    "cashflow": (
        "query_cash_flow_data",
        ("CFOToNP", "CFOToOR", "CFOToGr", "ebitToInterest"),
    ),
    "growth": (
        "query_growth_data",
        ("YOYNI", "YOYPNI", "YOYEquity", "YOYAsset", "YOYEPSBasic"),
    ),
}

_EXCHANGE_TAG: dict[str, str] = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


@dataclass(frozen=True)
class ReportPanel:
    """Wide report table: ``index=(symbol, period)``, plus the publication date."""

    frame: pd.DataFrame
    provider: str

    @property
    def periods(self) -> tuple[str, ...]:
        """Quarter labels, newest first."""
        if self.frame.empty:
            return ()
        return tuple(sorted(self.frame.index.get_level_values("period").unique(), reverse=True))

    @property
    def published(self) -> dict[str, str]:
        """``{"2025Q4": "2026-04-17"}`` — when each quarter became public."""
        column = self.frame["published"].dropna()
        return {
            str(period): str(value)
            for period, value in column.groupby(level="period").first().items()
        }

    def latest(self, symbol: str, as_of: date | str | None = None) -> pd.Series | None:
        """The newest report that was already public on ``as_of``.

        This is the only correct way to read a report for a backtest: asking for
        "the latest quarter" would hand 2026Q3 to a screen run in July.
        """
        # an empty panel has no levels at all, and asking one for its symbols
        # raises KeyError instead of answering "nothing collected yet"
        if self.frame.empty or "symbol" not in self.frame.index.names:
            return None
        if symbol not in self.frame.index.get_level_values("symbol"):
            return None
        own = self.frame.xs(symbol, level="symbol")
        if as_of is not None:
            limit = pd.Timestamp(as_of)
            own = own[own["published"] <= limit]
        if own.empty:
            return None
        row: pd.Series = own.sort_index().iloc[-1]
        return row


def quarters_back(count: int, today: date | None = None) -> list[tuple[int, int]]:
    """The last ``count`` *finished* quarters, newest first.

    A quarter that has not ended cannot have a report, and asking for it wastes
    four queries per symbol to learn nothing.
    """
    if count < 1:
        raise ValueError("count must be positive")
    moment = today or date.today()
    year = moment.year
    quarter = (moment.month - 1) // 3 + 1
    out: list[tuple[int, int]] = []
    for _ in range(count):
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
        out.append((year, quarter))
    return out


def _code(symbol: str) -> str:
    code, _, exchange = normalize_symbol(symbol).partition(".")
    tag = _EXCHANGE_TAG.get(exchange)
    if tag is None:
        raise ProviderError(f"{symbol}: BaoStock covers SH / SZ / BJ only")
    return f"{tag}.{code}"


def _number(text: str) -> float:
    """BaoStock reports everything as strings, with ``""`` for "no value"."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return float("nan")


def _report_text(value: Any) -> str:
    return "" if value is None else str(value)


def collect(
    symbols: list[str],
    quarters: int = 8,
    pause: float = 0.0,
    today: date | None = None,
) -> ReportPanel:
    """Fetch the last ``quarters`` finished quarters for every symbol.

    One query per table per quarter per symbol: thirty names over eight quarters
    is about a thousand round trips, which is why ``pause`` exists and why the
    result is cached rather than recomputed on every run.
    """
    if not symbols:
        raise ValueError("no symbols to fetch")

    try:
        import baostock
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ProviderUnavailable(
            "baostock is not installed. Install the collection extras with:\n"
            "    pip install -e '.[collect]'"
        ) from exc

    login = baostock.login()
    if getattr(login, "error_code", "0") != "0":
        raise ProviderError(f"baostock login failed: {login.error_msg}")

    wanted = quarters_back(quarters, today=today)
    rows: list[dict[str, Any]] = []
    try:
        for symbol in symbols:
            normalised = normalize_symbol(symbol)
            code = _code(normalised)
            for year, quarter in wanted:
                row = _one_quarter(baostock, normalised, code, year, quarter)
                if row is not None:
                    rows.append(row)
                if pause:
                    time.sleep(pause)
    finally:
        baostock.logout()

    return ReportPanel(frame=_assemble(rows), provider="baostock")


def _one_quarter(
    baostock: Any, symbol: str, code: str, year: int, quarter: int
) -> dict[str, Any] | None:
    """Merge the four tables for one quarter, or ``None`` if nothing was filed."""
    row: dict[str, Any] = {"symbol": symbol, "period": f"{year}Q{quarter}"}
    published = ""
    found = False
    for method, keep in _TABLES.values():
        result = getattr(baostock, method)(code=code, year=year, quarter=quarter)
        if getattr(result, "error_code", "1") != "0":
            continue  # not published, or the name was not listed then
        records: list[list[str]] = []
        while result.next():
            records.append(result.get_row_data())
        if not records:
            continue
        record = dict(zip(result.fields, records[0], strict=False))
        published = published or _report_text(record.get("pubDate"))
        for field in keep:
            row[field] = _number(_report_text(record.get(field)))
        found = True

    if not found or not published:
        return None
    row["published"] = published
    return row


def _assemble(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["published"] = pd.to_datetime(frame["published"], errors="coerce")
    frame = frame.dropna(subset=["published"])
    index = pd.MultiIndex.from_frame(frame[["symbol", "period"]], names=["symbol", "period"])
    frame = frame.drop(columns=["symbol", "period"]).set_index(index)
    return frame.sort_index()


def _path(directory: Path | str, provider: str = "baostock") -> Path:
    return Path(directory) / provider / "reports.parquet"


def store(panel: ReportPanel, directory: Path | str) -> Path:
    """Write the wide table to disk, replacing what was there."""
    target = _path(directory, panel.provider)
    target.parent.mkdir(parents=True, exist_ok=True)
    panel.frame.to_parquet(target)
    return target


def load(directory: Path | str, provider: str = "baostock") -> ReportPanel:
    """Read the stored table; an empty panel when nothing was collected yet."""
    target = _path(directory, provider)
    if not target.is_file():
        return ReportPanel(frame=pd.DataFrame(), provider=provider)
    return ReportPanel(frame=pd.read_parquet(target), provider=provider)


def merge(old: ReportPanel, new: ReportPanel) -> ReportPanel:
    """Keep both: a refetch should add quarters, not forget the ones already held.

    Restatements overwrite, because the later filing is the one that is true now.
    """
    if old.frame.empty:
        return new
    if new.frame.empty:
        return old
    combined = pd.concat([old.frame, new.frame])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    return ReportPanel(frame=combined, provider=new.provider)
