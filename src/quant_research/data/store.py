"""Local on-disk storage for collected daily bars.

Layout (one directory per provider, so two sources never overwrite each other)::

    data/raw/akshare/
        daily/600519.SH.parquet     # one file per symbol, date index
        _state.json                 # what was collected, when, and what failed
        _calendar.csv               # optional cached trading days

Writes are atomic (temp file + ``replace``) because the collector may be killed
mid-run by a scheduler, and a half-written parquet is worse than no file.

``Parquet`` is preferred; if ``pyarrow`` is unavailable the store silently falls
back to CSV so the pipeline still works on a minimal install.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.logging_setup import get_logger

logger = get_logger("data.store")

_SUPPORTED_FORMATS: tuple[str, ...] = ("parquet", "csv")
_STATE_VERSION = 1


@dataclass(frozen=True)
class UpsertResult:
    """What one symbol's write changed."""

    rows_fetched: int
    rows_added: int
    rows_total: int
    first_date: date | None
    last_date: date | None


@dataclass
class StoreState:
    """Persistent bookkeeping for one provider directory."""

    provider: str
    adjust: str = "qfq"
    format: str = "parquet"
    volume_unit: str = "手"
    version: int = _STATE_VERSION
    updated_at: str | None = None
    last_run_date: str | None = None
    symbols: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StoreState:
        known = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in known})

    def last_date(self, symbol: str) -> date | None:
        entry = self.symbols.get(symbol)
        if not entry:
            return None
        raw = entry.get("last_date")
        return date.fromisoformat(str(raw)) if raw else None


class LocalMarketStore:
    """Per-symbol files with incremental upserts and atomic replacement."""

    def __init__(self, root: str | Path, provider: str, *, fmt: str = "parquet") -> None:
        if fmt not in _SUPPORTED_FORMATS:
            raise ValueError(f"format must be one of {list(_SUPPORTED_FORMATS)}, got {fmt!r}")
        self.root = Path(root).expanduser()
        self.provider = provider
        self.base_dir = self.root / provider
        self.daily_dir = self.base_dir / "daily"
        self.state_path = self.base_dir / "_state.json"
        self.calendar_path = self.base_dir / "_calendar.csv"
        self.format = self._resolve_format(fmt)

    # --- paths ---------------------------------------------------------------

    def _resolve_format(self, requested: str) -> str:
        if requested != "parquet":
            return requested
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            logger.warning("pyarrow is not installed; falling back to CSV storage")
            return "csv"
        return "parquet"

    def path_for(self, symbol: str, fmt: str | None = None) -> Path:
        return self.daily_dir / f"{symbol}.{fmt or self.format}"

    # --- read / write --------------------------------------------------------

    def _read_path(self, path: Path) -> pd.DataFrame | None:
        if not path.is_file():
            return None
        try:
            if path.suffix == ".parquet":
                frame = pd.read_parquet(path)
                frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).normalize()
            else:
                frame = pd.read_csv(path, parse_dates=["date"], index_col="date")
                frame.index = pd.DatetimeIndex(frame.index).normalize()
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            logger.warning("unreadable data file %s (%s); it will be rewritten", path, exc)
            return None
        frame.index.name = "date"
        return frame.sort_index()

    def read(self, symbol: str) -> pd.DataFrame | None:
        """Return stored bars for ``symbol``, trying both formats for migration ease."""
        for fmt in (self.format, *(item for item in _SUPPORTED_FORMATS if item != self.format)):
            frame = self._read_path(self.path_for(symbol, fmt))
            if frame is not None:
                return frame
        return None

    def symbol_files(self) -> list[Path]:
        """Every stored symbol file, regardless of format."""
        if not self.daily_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.daily_dir.iterdir()
            if path.suffix.lstrip(".") in _SUPPORTED_FORMATS and not path.name.startswith(".")
        )

    def stored_symbols(self) -> list[str]:
        return [path.stem for path in self.symbol_files()]

    def upsert(self, symbol: str, bars: pd.DataFrame) -> UpsertResult:
        """Merge ``bars`` into the symbol file, keeping the newest value per date."""
        if bars.empty:
            existing = self.read(symbol)
            return UpsertResult(0, 0, 0 if existing is None else len(existing), None, None)

        existing = self.read(symbol)
        incoming = bars.sort_index()
        if existing is None:
            merged = incoming
            added = len(incoming)
        else:
            merged = pd.concat([existing, incoming])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            added = len(merged) - len(existing)

        self._write(symbol, merged)
        # Remove a stale counterpart when the configured format changed.
        for fmt in _SUPPORTED_FORMATS:
            stale = self.path_for(symbol, fmt)
            if stale.suffix.lstrip(".") != self.format and stale.is_file():
                stale.unlink(missing_ok=True)

        return UpsertResult(
            rows_fetched=len(incoming),
            rows_added=added,
            rows_total=len(merged),
            first_date=merged.index[0].date() if len(merged) else None,
            last_date=merged.index[-1].date() if len(merged) else None,
        )

    def _write(self, symbol: str, frame: pd.DataFrame) -> None:
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        target = self.path_for(symbol)
        temporary = target.with_name(target.name + ".tmp")

        if target.suffix == ".parquet":
            frame.to_parquet(temporary, index=True)
        else:
            frame.to_csv(temporary, index_label="date")
        os.replace(temporary, target)

    # --- state ---------------------------------------------------------------

    def load_state(self, *, adjust: str = "qfq") -> StoreState:
        if not self.state_path.is_file():
            return StoreState(provider=self.provider, adjust=adjust, format=self.format)
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("ignoring corrupt state file %s: %s", self.state_path, exc)
            return StoreState(provider=self.provider, adjust=adjust, format=self.format)

        state = StoreState.from_dict(payload)
        state.provider = self.provider
        state.format = self.format
        if not isinstance(state.symbols, dict):
            state.symbols = {}
        if not isinstance(state.failures, dict):
            state.failures = {}
        return state

    def save_state(self, state: StoreState) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)


__all__ = ["LocalMarketStore", "StoreState", "UpsertResult"]
