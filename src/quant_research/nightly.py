"""The nightly run: wait for the close, collect, check, then screen.

The order is the whole point. A screen against a half-landed day is worse than
no screen at all: it looks exactly like a good one, and it writes itself into
the ledger, where it will quietly drag the review numbers down for months. So
the run collects first, then asks one blunt question — is the day we care about
actually in the data? — and screens only when the answer is yes.

That check is not paranoia. A vendor's same-day prices land when they land, and
a screen run at 15:05 against a day that finished updating at 17:00 will produce
a confident, complete-looking, wrong list.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from time import sleep as block

import pandas as pd

from quant_research import overseas as overseas_mod
from quant_research import sector_flow as sector_flow_mod
from quant_research.collector import DEFAULT_TIMEZONE, collect_once, now_in
from quant_research.data import index as index_mod
from quant_research.data.cache import (
    build_panel_cache,
    cache_path,
    load_panel_cache,
    refresh_symbols,
)
from quant_research.data.calendar import TradingCalendar
from quant_research.data.providers import get_provider
from quant_research.data.providers.base import MarketDataProvider
from quant_research.data.store import LocalMarketStore
from quant_research.logging_setup import get_logger
from quant_research.scoring import screen
from quant_research.scoring.ledger import ScreenLedger

logger = get_logger("nightly")

DEFAULT_REPORT_DIRECTORY = Path("reports/nightly")

__all__ = ["DEFAULT_REPORT_DIRECTORY", "NightlyReport", "run_nightly", "write_report"]


@dataclass
class NightlyReport:
    """What one night did, including the nights that deliberately did nothing."""

    provider: str
    adjust: str
    as_of: str
    screened: bool
    picks: int = 0
    collected: dict[str, object] = field(default_factory=dict)
    environment: dict[str, object] = field(default_factory=dict)
    note: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "adjust": self.adjust,
            "as_of": self.as_of,
            "screened": self.screened,
            "picks": self.picks,
            "collected": dict(self.collected),
            "environment": dict(self.environment),
            "note": self.note,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _refresh_environment(data_root: Path) -> dict[str, object]:
    """外围快照与指数日线：环境数据跟着每晚更新。

    两个都各自容错——外围抓取失败只是战法页少一块标注，但「今晚没抓到、
    为什么没抓到」会被如实记录，而不是悄悄跳过。
    """
    result: dict[str, object] = {}
    try:
        snapshot = overseas_mod.fetch()
        overseas_mod.save(Path(data_root), snapshot)
        result["overseas"] = {"quotes": len(snapshot["quotes"]), "missing": snapshot["missing"]}
    except Exception as exc:  # noqa: BLE001 - 环境刷新失败不阻断夜间采集
        result["overseas"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        for symbol in index_mod.INDEXES:
            frame = index_mod.refresh(Path(data_root), symbol)
            result["index"] = {"symbol": symbol, "as_of": str(frame.index[-1].date())}
    except Exception as exc:  # noqa: BLE001
        result["index"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        snapshot = sector_flow_mod.fetch()
        sector_flow_mod.save(Path(data_root), snapshot)
        result["sector_flow"] = {
            "sectors": len(snapshot["sectors"]),
            "missing": snapshot["missing"],
        }
    except Exception as exc:  # noqa: BLE001
        result["sector_flow"] = {"error": f"{type(exc).__name__}: {exc}"}
    return result


def run_nightly(
    *,
    provider_name: str = "baostock",
    provider: MarketDataProvider | None = None,
    data_root: str | Path = "data",
    symbols: Sequence[str] | None = None,
    top_n: int = 20,
    min_history: int = 60,
    min_amount: float | None = None,
    lookback_days: int = 10,
    pause: float = 0.35,
    wait_until: time | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    calendar: TradingCalendar | None = None,
    today: date | None = None,
    sleep: Callable[[float], None] = block,
    ledger: ScreenLedger | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> NightlyReport:
    """Collect the day, then screen it — or say plainly why not.

    ``wait_until`` holds the run back until the vendor has had time to publish.
    ``pause`` defaults to the sweep floor rather than zero: a full-universe run
    that hammers the source gets cut off, and a nightly job nobody notices
    failing is worse than a slow one.
    """
    calendar = calendar or TradingCalendar()
    moment = now_in(timezone)
    day = today or moment.date()
    started = moment.isoformat(timespec="seconds")

    if wait_until is not None:
        delay = _seconds_until(wait_until, moment)
        if delay > 0:
            logger.info("waiting %.0fs until %s for the day to land", delay, wait_until)
            sleep(delay)

    if not calendar.is_trading_day(day):
        return _done(
            provider_name,
            "unknown",
            day,
            started,
            timezone,
            screened=False,
            note=f"{day} is not a trading day; nothing to screen",
        )

    store = LocalMarketStore(Path(data_root).expanduser() / "raw", provider_name)
    targets = list(symbols) if symbols else store.stored_symbols()
    if not targets:
        return _done(
            provider_name,
            "unknown",
            day,
            started,
            timezone,
            screened=False,
            note="no symbols to collect; pass symbols or store some first",
        )

    source = provider or get_provider(provider_name)
    adjust = str(getattr(source, "adjust", "none"))
    collection = collect_once(
        provider=source,
        store=store,
        symbols=targets,
        lookback_days=lookback_days,
        pause=pause,
        calendar=calendar,
        timezone=timezone,
        sleep=sleep,
        should_stop=should_stop,
    )

    # Bring the cache up to date with just the files that changed.
    if not cache_path(data_root, provider_name, adjust).is_file():
        build_panel_cache(data_root, provider_name, adjust)
    else:
        refresh_symbols(data_root, provider_name, adjust, targets)

    environment = _refresh_environment(data_root)

    panel = load_panel_cache(data_root, provider_name, adjust)
    if pd.Timestamp(day) not in panel.prices.index:
        latest = panel.prices.index.max().date()
        return _done(
            provider_name,
            adjust,
            day,
            started,
            timezone,
            screened=False,
            collected=collection.to_dict(),
            environment=environment,
            note=f"{day} has not landed yet (latest is {latest}); refusing to screen",
        )

    result = screen(
        panel,
        as_of=day,
        top_n=top_n,
        min_history=min_history,
        min_amount=min_amount,
    )
    (ledger or ScreenLedger()).record(
        result,
        provider=provider_name,
        adjust=adjust,
        params={"top_n": top_n, "min_history": min_history, "min_amount": min_amount},
        note="nightly",
    )
    logger.info(
        "nightly screen for %s: %d picks from %d candidates",
        day,
        len(result.picks),
        result.universe,
    )
    return _done(
        provider_name,
        adjust,
        day,
        started,
        timezone,
        screened=True,
        picks=len(result.picks),
        collected=collection.to_dict(),
        environment=environment,
        note=f"screened {day}: {len(result.picks)} picks",
    )


def write_report(
    report: NightlyReport,
    directory: str | Path = DEFAULT_REPORT_DIRECTORY,
    timezone: str = DEFAULT_TIMEZONE,
) -> Path:
    """Archive the night as JSON, including the nights that screened nothing."""
    folder = Path(directory).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now_in(timezone).strftime("%Y%m%dT%H%M%S")
    path = folder / f"{stamp}_{report.provider}.json"
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def _seconds_until(target: time, moment: datetime) -> float:
    due = moment.replace(
        hour=target.hour,
        minute=target.minute,
        second=getattr(target, "second", 0),
        microsecond=0,
    )
    return max(0.0, (due - moment).total_seconds())


def _done(
    provider: str,
    adjust: str,
    day: date,
    started: str,
    timezone: str,
    *,
    screened: bool,
    picks: int = 0,
    collected: dict[str, object] | None = None,
    environment: dict[str, object] | None = None,
    note: str = "",
) -> NightlyReport:
    if note:
        logger.info(note)
    return NightlyReport(
        provider=provider,
        adjust=adjust,
        as_of=str(day),
        screened=screened,
        picks=picks,
        collected=collected or {},
        environment=environment or {},
        note=note,
        started_at=started,
        finished_at=now_in(timezone).isoformat(timespec="seconds"),
    )
