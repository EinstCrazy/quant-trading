"""The nightly ledger: every short list we handed out, kept for later.

A screen is an opinion. Whether it was any good is only knowable afterwards, so
each day's picks are written down with everything needed to score them later —
the date, the parameters, the scores, and what was rejected and why.

One record per day. Running the screen twice on the same evening overwrites
rather than appends: there is one opinion per day, and a ledger with two of them
is only confusing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from quant_research.data.loader import PricePanel
from quant_research.logging_setup import get_logger
from quant_research.scoring.screen import ScreenResult

logger = get_logger("scoring.ledger")

DEFAULT_PATH = Path("reports/screening/index.jsonl")

__all__ = ["DEFAULT_PATH", "ScreenLedger", "ScreenRecord", "review"]


@dataclass(frozen=True)
class ScreenRecord:
    """One day's short list, as it was handed out."""

    as_of: str
    created_at: str
    provider: str = "unknown"
    adjust: str = "unknown"
    picks: dict[str, float] = field(default_factory=dict)
    universe: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    params: dict[str, object] = field(default_factory=dict)
    note: str = ""

    @property
    def symbols(self) -> list[str]:
        return list(self.picks)

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of,
            "created_at": self.created_at,
            "provider": self.provider,
            "adjust": self.adjust,
            "picks": dict(self.picks),
            "universe": self.universe,
            "dropped": dict(self.dropped),
            "params": dict(self.params),
            "note": self.note,
        }


class ScreenLedger:
    """An append-mostly JSONL ledger of screens.

    Reads on every call so a record written by another process — the workbench,
    a second shell — shows up without a restart.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_PATH

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        result: ScreenResult,
        *,
        provider: str = "unknown",
        adjust: str = "unknown",
        params: dict[str, object] | None = None,
        note: str = "",
    ) -> ScreenRecord:
        """Write down one day's screen, replacing any earlier record for that day."""
        entry = ScreenRecord(
            as_of=str(pd.Timestamp(result.as_of).date()),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            provider=provider,
            adjust=adjust,
            picks={str(symbol): float(score) for symbol, score in result.picks["score"].items()},
            universe=result.universe,
            dropped=dict(result.dropped),
            params=dict(params or {}),
            note=note,
        )
        records = [item for item in self._read_all() if item.as_of != entry.as_of]
        records.append(entry)
        records.sort(key=lambda item: item.as_of)
        self._write_all(records)
        logger.info(
            "recorded screen for %s: %d picks from %d candidates",
            entry.as_of,
            len(entry.symbols),
            entry.universe,
        )
        return entry

    def recent(self, limit: int | None = 20) -> list[ScreenRecord]:
        """Newest first; ``limit=None`` returns every record."""
        records = sorted(self._read_all(), key=lambda item: item.as_of, reverse=True)
        return records if limit is None else records[:limit]

    def get(self, as_of: str) -> ScreenRecord | None:
        wanted = str(pd.Timestamp(as_of).date())
        for record in self._read_all():
            if record.as_of == wanted:
                return record
        return None

    def _read_all(self) -> list[ScreenRecord]:
        if not self._path.is_file():
            return []
        items: list[ScreenRecord] = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(ScreenRecord(**json.loads(line)))
            except (json.JSONDecodeError, TypeError) as exc:
                # A half-written line is not worth losing the other thousand.
                logger.warning("skipping malformed screen line %d: %s", number, exc)
        return items

    def _write_all(self, records: list[ScreenRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(item.to_dict(), ensure_ascii=False) for item in records)
        # Write-then-replace: a crash mid-write must not truncate the ledger.
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(payload + ("\n" if payload else ""), encoding="utf-8")
        tmp.replace(self._path)


def review(
    ledger: ScreenLedger,
    panel: PricePanel,
    horizon: int = 5,
    entry_field: str = "open",
) -> pd.DataFrame:
    """Score yesterday's opinions against what actually happened.

    Picks are assumed to be bought at the **next** session's open — the same
    assumption the backtester makes, so the ledger and the backtest are
    measuring the same thing — and sold ``horizon`` sessions later at the close.

    Comparing against the equal-weighted market over the same window says
    whether the list beat simply holding everything, which is the honest
    question. Two caveats travel with every number here: the sample is one list
    of names, and the names are the ones still trading today, so a ledger
    stretching back over delisted names will flatter itself.

    Days whose window has not closed yet are left out entirely — a partial
    holding period would quietly bias the average towards whatever happened
    this morning.
    """
    if horizon < 1:
        raise ValueError("horizon must be at least 1 session")

    prices = panel.prices.sort_index()
    entries = (
        panel.field(entry_field).reindex(index=prices.index, columns=prices.columns)
        if panel.has_field(entry_field)
        else prices
    )

    rows: list[dict[str, object]] = []
    for record in ledger.recent(limit=None):
        symbols = record.symbols
        if not symbols or record.as_of not in prices.index:
            continue

        position = int(prices.index.get_loc(record.as_of))
        enter_at = position + 1
        exit_at = enter_at + horizon
        if exit_at >= len(prices.index):
            continue

        bought = entries.iloc[enter_at].reindex(symbols)
        sold = prices.iloc[exit_at].reindex(symbols)
        returns = (sold / bought - 1.0).dropna()
        if returns.empty:
            continue

        market = (prices.iloc[exit_at] / entries.iloc[enter_at] - 1.0).dropna()
        benchmark = float(market.mean()) if not market.empty else float("nan")

        rows.append(
            {
                "as_of": record.as_of,
                "picks": len(returns),
                "entry": str(prices.index[enter_at].date()),
                "exit": str(prices.index[exit_at].date()),
                "mean_return": float(returns.mean()),
                "median_return": float(returns.median()),
                "win_rate": float((returns > 0).mean()),
                "benchmark": benchmark,
                "excess": float(returns.mean()) - benchmark,
                "beat_market": float((returns > benchmark).mean()),
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "as_of",
                "picks",
                "entry",
                "exit",
                "mean_return",
                "median_return",
                "win_rate",
                "benchmark",
                "excess",
                "beat_market",
            ]
        )
    return frame.sort_values("as_of").reset_index(drop=True)
