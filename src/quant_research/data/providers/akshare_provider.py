"""A-share daily bars from AKShare (Eastmoney endpoints under the hood).

AKShare is an optional dependency: it pulls a fairly large tree of packages and
its upstream interfaces change often, so it is imported lazily and its columns are
mapped defensively. When the package is missing the collector fails with
``ProviderUnavailable`` instead of an ``ImportError`` traceback.
"""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar

import pandas as pd

from quant_research.data.providers.base import (
    MarketDataProvider,
    ProviderError,
    ProviderUnavailable,
    SourceUnreachable,
    explain,
    is_transport_error,
    normalise_bars,
    normalize_symbol,
    to_bare_symbol,
)

# Verified against akshare 1.19.1 / stock_zh_a_hist on 2026-10-21.
_COLUMN_MAP: dict[str, str] = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_change",
}

# Project vocabulary -> AKShare's ``adjust`` argument.
_ADJUST_MAP: dict[str, str] = {
    "qfq": "qfq",
    "forward": "qfq",
    "hfq": "hfq",
    "backward": "hfq",
    "none": "",
    "": "",
}

_EARLIEST = "19900101"


class AkshareProvider(MarketDataProvider):
    """Daily bars for Shanghai, Shenzhen and Beijing listings."""

    name: ClassVar[str] = "akshare"

    def __init__(self, adjust: str = "qfq") -> None:
        if adjust not in _ADJUST_MAP:
            raise ValueError(f"adjust must be one of {sorted(_ADJUST_MAP)}, got {adjust!r}")
        self.adjust = adjust

    @staticmethod
    def _akshare() -> Any:
        try:
            import akshare
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ProviderUnavailable(
                "akshare is not installed. Install the collection extras with:\n"
                "    pip install -e '.[collect]'"
            ) from exc
        return akshare

    def list_symbols(self) -> list[str]:
        """Return every listed A share.

        Note: this endpoint is slow (tens of seconds) and occasionally stalls, so the
        collector only calls it for ``--all`` and always falls back to the symbols it
        already tracks when it fails.
        """
        akshare = self._akshare()
        try:
            table = akshare.stock_info_a_code_name()
        except Exception as exc:
            raise ProviderError(f"failed to list A-share symbols: {exc}") from exc

        if table is None or table.empty or "code" not in table.columns:
            raise ProviderError("stock_info_a_code_name returned no usable 'code' column")
        return [normalize_symbol(str(code).zfill(6)) for code in table["code"]]

    def fetch_daily(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, pd.DataFrame]:
        akshare = self._akshare()
        start_text = pd.Timestamp(start).strftime("%Y%m%d") if start else _EARLIEST
        end_text = pd.Timestamp(end or date.today()).strftime("%Y%m%d")
        adjust = _ADJUST_MAP[self.adjust]

        frames: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            normalised = normalize_symbol(symbol)
            try:
                raw = akshare.stock_zh_a_hist(
                    symbol=to_bare_symbol(normalised),
                    period="daily",
                    start_date=start_text,
                    end_date=end_text,
                    adjust=adjust,
                )
            except Exception as exc:
                # wrap it as ProviderError, but preserve "not interrupting unexpectedly"
                if is_transport_error(exc):
                    raise SourceUnreachable(
                        explain(exc, f"{self.name}（东方财富行情接口）")
                    ) from exc
                raise ProviderError(f"{normalised}: {type(exc).__name__}: {exc}") from exc

            if raw is None or raw.empty:
                continue  # no trading history in the window

            missing = [column for column in ("日期", "开盘", "收盘", "最高", "最低", "成交量")]
            if [column for column in missing if column not in raw.columns]:
                raise ProviderError(f"{normalised}: unexpected AKShare columns {list(raw.columns)}")

            frame = raw.rename(columns=_COLUMN_MAP).set_index("date")
            frames[normalised] = normalise_bars(frame, source=f"{self.name}:{normalised}")
        return frames


__all__ = ["AkshareProvider"]
