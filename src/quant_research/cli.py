"""Command line entry point for the daily A-share collector.

Usable three ways, all equivalent:

    quant-collect --symbols 600519.SH,000001.SZ
    python -m quant_research.cli --symbols 600519.SH
    python scripts/collect_a_shares.py --symbols 600519.SH
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time
from pathlib import Path

from quant_research.collector import (
    CollectionReport,
    collect_once,
    resolve_symbols,
    run_daily_loop,
    write_report,
)
from quant_research.config import CollectionSettings, load_settings
from quant_research.data.calendar import TradingCalendar
from quant_research.data.providers import (
    AVAILABLE_PROVIDERS,
    MarketDataProvider,
    get_provider,
)
from quant_research.data.store import LocalMarketStore
from quant_research.logging_setup import configure_logging, get_logger

logger = get_logger("cli")

_EXIT_OK = 0
_EXIT_FAILED = 1
_EXIT_USAGE = 2


def _parse_date(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from exc


def _parse_time(text: str) -> time:
    try:
        hour, minute = (int(part) for part in text.split(":", maxsplit=1))
        return time(hour=hour, minute=minute)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected HH:MM, got {text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-collect",
        description=(
            "Download A-share daily bars to local disk, once or as a daily daemon. "
            "Defaults come from config/settings.yaml (the 'collection' section)."
        ),
    )
    parser.add_argument("--provider", choices=AVAILABLE_PROVIDERS, help="data source")
    parser.add_argument("--symbols", help="comma separated codes, e.g. 600519.SH,000001.SZ")
    parser.add_argument("--symbols-file", type=Path, help="file with one code per line")
    parser.add_argument(
        "--all",
        action="store_true",
        help="ask the provider for the full market list (slow, may stall)",
    )
    parser.add_argument("--start", type=_parse_date, help="backfill start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_date, help="end date (default: today)")
    parser.add_argument("--full", action="store_true", help="ignore the stored last date")
    parser.add_argument("--lookback-days", type=int, help="trailing window re-fetched each run")
    parser.add_argument("--adjust", choices=("qfq", "hfq", "none"), help="price adjustment")
    parser.add_argument("--output-dir", type=Path, help="store root (default data/raw)")
    parser.add_argument("--format", choices=("parquet", "csv"), help="storage format")
    parser.add_argument("--daemon", action="store_true", help="keep running, collecting daily")
    parser.add_argument("--at", type=_parse_time, help="daemon trigger time (HH:MM)")
    parser.add_argument("--timezone", help="timezone for scheduling (default Asia/Shanghai)")
    parser.add_argument("--retries", type=int, help="retries per symbol on provider errors")
    parser.add_argument("--pause", type=float, help="seconds to sleep between symbols")
    parser.add_argument(
        "--refresh-calendar",
        action="store_true",
        help="download the trading calendar (slow) instead of using the cached one",
    )
    parser.add_argument("--report-dir", type=Path, help="where run reports are archived")
    parser.add_argument("--no-report", action="store_true", help="do not archive a JSON report")
    parser.add_argument("--dry-run", action="store_true", help="fetch but never write to disk")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    return parser


def _configured_symbols(args: argparse.Namespace, settings: CollectionSettings) -> list[str]:
    collected: list[str] = []
    if args.symbols:
        collected.extend(part.strip() for part in args.symbols.split(",") if part.strip())
    if args.symbols_file:
        path = args.symbols_file.expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"symbols file not found: {path}")
        collected.extend(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return collected or list(settings.symbols)


def _pick_symbols(
    args: argparse.Namespace,
    settings: CollectionSettings,
    provider: MarketDataProvider,
    store: LocalMarketStore,
) -> list[str]:
    configured = _configured_symbols(args, settings)
    if configured:
        return resolve_symbols(provider, configured)

    if args.all:
        try:
            return provider.list_symbols()
        except Exception as exc:  # noqa: BLE001 - any upstream failure is acceptable here
            logger.warning("full symbol list unavailable (%s); using stored symbols", exc)

    stored = store.stored_symbols()
    if stored:
        logger.info("no symbols configured; refreshing %d already-stored symbols", len(stored))
    return stored


def _resolve_calendar(args: argparse.Namespace, store: LocalMarketStore) -> TradingCalendar:
    calendar = TradingCalendar.load_or_weekday(store.calendar_path)
    if not args.refresh_calendar:
        if not calendar.is_exact:
            logger.info("no cached trading calendar; weekends are skipped, holidays are not")
        return calendar

    try:
        refreshed = TradingCalendar.from_akshare()
    except RuntimeError as exc:
        logger.warning("calendar refresh failed, keeping %s: %s", calendar.source, exc)
        return calendar

    refreshed.save(store.calendar_path)
    logger.info("trading calendar refreshed: %d days", len(refreshed.days or ()))
    return refreshed


def _run(
    args: argparse.Namespace,
    settings: CollectionSettings,
    store: LocalMarketStore,
    calendar: TradingCalendar,
) -> CollectionReport:
    timezone = args.timezone or settings.timezone
    provider = get_provider(
        args.provider or settings.provider,
        adjust=args.adjust or settings.adjust,
    )
    symbols = _pick_symbols(args, settings, provider, store)
    if not symbols:
        raise ValueError(
            "nothing to collect: pass --symbols, --all, or set collection.symbols "
            "in config/settings.yaml"
        )

    report = collect_once(
        provider=provider,
        store=store,
        symbols=symbols,
        start=args.start,
        end=args.end,
        full=args.full,
        lookback_days=(
            args.lookback_days if args.lookback_days is not None else settings.lookback_days
        ),
        calendar=calendar,
        retries=args.retries if args.retries is not None else settings.retries,
        pause=args.pause if args.pause is not None else settings.pause_seconds,
        timezone=timezone,
        dry_run=args.dry_run,
    )

    if not args.no_report:
        target = args.report_dir or settings.report_directory
        path = write_report(report, target, timezone=timezone)
        logger.info("report archived to %s", path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(getattr(logging, str(args.log_level).upper(), logging.INFO))

    try:
        settings = load_settings().collection
    except (FileNotFoundError, ValueError, TypeError) as exc:
        logger.error("cannot read config/settings.yaml: %s", exc)
        return _EXIT_USAGE

    store = LocalMarketStore(
        args.output_dir or settings.output_directory,
        args.provider or settings.provider,
        fmt=args.format or settings.format,
    )
    calendar = _resolve_calendar(args, store)

    if args.daemon:
        timezone = args.timezone or settings.timezone
        at = args.at or settings.at_time()
        logger.info("daemon mode: collecting every trading day at %s (%s)", at, timezone)
        run_daily_loop(
            lambda: _run(args, settings, store, calendar),
            at=at,
            timezone=timezone,
            calendar=calendar,
        )
        return _EXIT_OK

    try:
        report = _run(args, settings, store, calendar)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return _EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - surface a clean message, not a traceback
        logger.error("collection aborted: %s: %s", type(exc).__name__, exc)
        return _EXIT_FAILED

    return _EXIT_OK if report.ok else _EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover - module execution path
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
