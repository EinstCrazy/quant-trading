"""Daily A-share collection loop.

Responsibilities, in order: decide whether today needs work, resolve the symbol
list, fetch one symbol at a time (so one broken ticker cannot sink the run), merge
into the local store, and record what happened.

Everything here is synchronous and idempotent: running it twice a day is safe, and
re-running after a failure simply retries the symbols that are still marked failed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from quant_research.data.calendar import TradingCalendar
from quant_research.data.providers.base import MarketDataProvider, ProviderError, normalize_symbol
from quant_research.data.store import LocalMarketStore, StoreState
from quant_research.logging_setup import get_logger

logger = get_logger("collector")

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LOOKBACK_DAYS = 10


@dataclass
class CollectionReport:
    """Outcome of one collection run, serialisable for archiving."""

    provider: str
    adjust: str
    requested: int
    started_at: str
    finished_at: str = ""
    calendar: str = "weekday"
    updated: dict[str, int] = field(default_factory=dict)
    added: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def touched(self) -> int:
        return len(self.updated)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "adjust": self.adjust,
            "requested": self.requested,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "calendar": self.calendar,
            "updated": dict(self.updated),
            "added": dict(self.added),
            "skipped": dict(self.skipped),
            "failed": dict(self.failed),
            "note": self.note,
            "summary": {
                "updated": len(self.updated),
                "rows_added": sum(self.added.values()),
                "skipped": len(self.skipped),
                "failed": len(self.failed),
            },
        }


def now_in(timezone: str = DEFAULT_TIMEZONE) -> datetime:
    return datetime.now(ZoneInfo(timezone))


def resolve_symbols(
    provider: MarketDataProvider,
    configured: Sequence[str] = (),
    *,
    include_all: bool = False,
) -> list[str]:
    """Configured list wins; ``include_all`` asks the source; otherwise nothing.

    The order matters: an explicit list is reproducible offline, while the full
    market list needs the network and can be slow or unavailable.
    """
    if configured:
        seen: dict[str, None] = {}
        for item in configured:
            seen.setdefault(normalize_symbol(item), None)
        return list(seen)
    if include_all:
        return provider.list_symbols()
    return []


def is_due(
    now: datetime,
    at: clock_time,
    last_run: date | None,
    calendar: TradingCalendar,
) -> bool:
    """True when the daily job should fire: trading day, past ``at``, not run yet."""
    today = now.date()
    if last_run == today:
        return False
    if now.time() < at:
        return False
    return calendar.is_trading_day(today)


def _fetch_with_retry(
    provider: MarketDataProvider,
    symbols: list[str],
    start: date | None,
    end: date | None,
    retries: int,
    backoff: float,
    sleep: Callable[[float], None],
) -> dict[str, pd.DataFrame]:
    attempt = 0
    while True:
        try:
            return provider.fetch_daily(symbols, start, end)
        except ProviderError as exc:
            attempt += 1
            if attempt > retries:
                raise
            wait = backoff ** (attempt - 1)
            logger.warning(
                "fetch failed for %s (attempt %d/%d), retrying in %.1fs: %s",
                symbols,
                attempt,
                retries,
                wait,
                exc,
            )
            sleep(wait)


def collect_once(
    *,
    provider: MarketDataProvider,
    store: LocalMarketStore,
    symbols: Sequence[str],
    start: date | None = None,
    end: date | None = None,
    full: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    calendar: TradingCalendar | None = None,
    retries: int = 2,
    backoff: float = 2.0,
    pause: float = 0.0,
    timezone: str = DEFAULT_TIMEZONE,
    sleep: Callable[[float], None] = time.sleep,
    dry_run: bool = False,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> CollectionReport:
    """Collect one day (or a backfill range) into the local store.

    ``lookback_days`` re-fetches a short trailing window on every run. This is not
    laziness: forward-adjusted prices change retroactively after a dividend or a
    split, and re-reading the tail keeps recent history consistent with the vendor
    without rewriting years of data each night.

    ``on_progress(done, total, symbol)`` fires before each symbol is fetched, so a
    caller can show progress. ``should_stop`` is polled the same way: when it turns
    true the run stops after the current symbol and reports what it finished — a
    full-universe backfill is otherwise impossible to interrupt.
    """
    calendar = calendar or TradingCalendar()
    today = end or now_in(timezone).date()
    report = CollectionReport(
        provider=provider.name,
        adjust=str(getattr(provider, "adjust", "none")),
        requested=len(symbols),
        started_at=now_in(timezone).isoformat(timespec="seconds"),
        calendar=calendar.source,
    )

    if not calendar.is_trading_day(today):
        report.note = f"{today} is not a trading day; nothing to collect"
        report.finished_at = now_in(timezone).isoformat(timespec="seconds")
        logger.info(report.note)
        return report

    state: StoreState = store.load_state(adjust=report.adjust)
    targets = list(dict.fromkeys(normalize_symbol(item) for item in symbols))

    for index, symbol in enumerate(targets, start=1):
        if should_stop is not None and should_stop():
            report.note = f"stopped after {index - 1}/{len(targets)} symbols"
            logger.warning("collection stopped early: %s", report.note)
            break
        if on_progress is not None:
            on_progress(index - 1, len(targets), symbol)

        window_start = start
        if not full and start is None:
            last = state.last_date(symbol)
            if last is not None:
                window_start = last - timedelta(days=lookback_days)

        try:
            frames = _fetch_with_retry(
                provider, [symbol], window_start, end, retries, backoff, sleep
            )
        except Exception as exc:  # noqa: BLE001 - isolate per-symbol failures
            message = f"{type(exc).__name__}: {exc}"
            report.failed[symbol] = message
            state.failures[symbol] = message
            logger.error("collection failed for %s: %s", symbol, message)
            continue

        bars = frames.get(symbol)
        if bars is None or bars.empty:
            report.skipped[symbol] = "no rows in window"
            state.failures.pop(symbol, None)
            continue

        if dry_run:
            report.updated[symbol] = len(bars)
            report.added[symbol] = len(bars)
            continue

        result = store.upsert(symbol, bars)
        report.updated[symbol] = result.rows_fetched
        report.added[symbol] = result.rows_added
        state.symbols[symbol] = {
            "first_date": result.first_date.isoformat() if result.first_date else None,
            "last_date": result.last_date.isoformat() if result.last_date else None,
            "rows": result.rows_total,
            "updated_at": now_in(timezone).isoformat(timespec="seconds"),
        }
        state.failures.pop(symbol, None)
        logger.info(
            "%s: +%d rows (total %d) through %s",
            symbol,
            result.rows_added,
            result.rows_total,
            result.last_date,
        )
        if pause:
            sleep(pause)

    if on_progress is not None:
        on_progress(len(targets), len(targets), "")

    if not dry_run:
        state.updated_at = now_in(timezone).isoformat(timespec="seconds")
        if not report.failed:
            # Only a clean run marks the day as done; a failed run stays eligible
            # for a retry if the operator invokes the collector again.
            state.last_run_date = today.isoformat()
        store.save_state(state)

    report.finished_at = now_in(timezone).isoformat(timespec="seconds")
    logger.info(
        "collection finished: %d updated, %d skipped, %d failed",
        len(report.updated),
        len(report.skipped),
        len(report.failed),
    )
    return report


def write_report(
    report: CollectionReport, directory: str | Path, timezone: str = DEFAULT_TIMEZONE
) -> Path:
    """Archive a run as JSON under ``reports/collection/<timestamp>_<provider>.json``."""
    target_dir = Path(directory).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_in(timezone).strftime("%Y%m%dT%H%M%S")
    path = target_dir / f"{stamp}_{report.provider}.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_daily_loop(
    collect: Callable[[], CollectionReport],
    *,
    at: Any,
    timezone: str = DEFAULT_TIMEZONE,
    calendar: TradingCalendar | None = None,
    tick_seconds: int = 60,
    max_runs: int | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Fire ``collect`` once per trading day at ``at``; return the number of runs.

    The in-memory guard prevents double runs within one process; the store's
    ``last_run_date`` covers the case where the process is restarted.
    """
    calendar = calendar or TradingCalendar()
    clock = now or (lambda: now_in(timezone))
    last_attempt: date | None = None
    runs = 0

    while max_runs is None or runs < max_runs:
        moment = clock()
        if is_due(moment, at, last_attempt, calendar):
            last_attempt = moment.date()
            runs += 1
            logger.info("starting scheduled collection for %s", moment.date())
            try:
                collect()
            except Exception:
                logger.exception("scheduled collection raised; daemon keeps running")
        sleep(tick_seconds)
    return runs


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_TIMEZONE",
    "CollectionReport",
    "collect_once",
    "is_due",
    "now_in",
    "resolve_symbols",
    "run_daily_loop",
    "write_report",
]
