"""Command line entry point for the nightly run.

    quant-nightly --provider baostock                # wait for the close, then collect and screen
    quant-nightly --provider baostock --now          # skip the wait (the landing check still applies)
    quant-nightly --provider baostock --daemon       # every trading day, at collection.schedule_at

The wait is not decoration: same-day prices land when the vendor has finished
with them, and screening before that produces a list that looks finished and is
not. ``--now`` still checks that the day is actually in the data.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from quant_research.collector import run_daily_loop
from quant_research.config import load_settings
from quant_research.logging_setup import get_logger
from quant_research.nightly import DEFAULT_REPORT_DIRECTORY, run_nightly, write_report

logger = get_logger("nightly")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-nightly",
        description="Collect the day after the close, then screen it. Defaults come from config/settings.yaml.",
    )
    parser.add_argument("--provider", help="data source (default: settings)")
    parser.add_argument("--data", help="data root holding raw/ and cache/ (default: settings)")
    parser.add_argument("--adjust", help="price adjustment, e.g. qfq (default: settings)")
    parser.add_argument(
        "--at",
        help="hold the run until this local time, e.g. 18:30 (default: collection.schedule_at)",
    )
    parser.add_argument("--now", action="store_true", help="do not wait, run immediately")
    parser.add_argument(
        "--daemon", action="store_true", help="stay up and run once per trading day at --at"
    )
    parser.add_argument("--max-runs", type=int, default=None, help="stop after N runs (daemon)")
    parser.add_argument("--top-n", type=int, default=20, help="how many names to keep")
    parser.add_argument("--min-history", type=int, default=60, help="sessions of history required")
    parser.add_argument(
        "--min-amount", type=float, default=None, help="drop names below this average turnover"
    )
    parser.add_argument("--symbols", help="comma separated; defaults to everything stored")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=10,
        help="re-fetch this trailing window (adjusted prices change retroactively)",
    )
    parser.add_argument("--pause", type=float, default=0.35, help="seconds between requests")
    parser.add_argument(
        "--report-dir", help=f"where to archive runs (default: {DEFAULT_REPORT_DIRECTORY})"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()

    provider = args.provider or settings.collection.provider
    root = args.data or settings.data.directory
    at_text = args.at or settings.collection.schedule_at
    wait = None if (args.now or args.daemon) else datetime.strptime(at_text, "%H:%M").time()
    symbols = (
        [item.strip() for item in args.symbols.split(",") if item.strip()] if args.symbols else None
    )

    def one_run() -> object:
        report = run_nightly(
            provider_name=provider,
            data_root=root,
            symbols=symbols,
            top_n=args.top_n,
            min_history=args.min_history,
            min_amount=args.min_amount,
            lookback_days=args.lookback_days,
            pause=args.pause,
            wait_until=wait,
        )
        path = write_report(report, args.report_dir or DEFAULT_REPORT_DIRECTORY)
        print(
            f"{report.as_of}｜{'已打分 ' + str(report.picks) + ' 只' if report.screened else '未打分'}"
            f"｜{report.note}"
        )
        print(f"报告 → {path}")
        return report

    try:
        if args.daemon:
            runs = run_daily_loop(
                one_run,
                at=datetime.strptime(at_text, "%H:%M").time(),
                timezone=settings.collection.timezone,
                max_runs=args.max_runs,
            )
            print(f"常驻结束，共执行 {runs} 次")
        else:
            one_run()
        return 0
    except (ValueError, FileNotFoundError, KeyError) as exc:
        logger.error("nightly failed: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
