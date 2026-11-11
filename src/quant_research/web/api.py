"""HTTP routes: thin orchestration over the research library.

Every number returned here was computed by ``quant_research`` itself. This module
validates input, calls library functions and serialises the result — it contains no
pricing logic of its own, so the UI can never disagree with a notebook.
"""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from openpyxl.utils import get_column_letter

from quant_research import holdings as holdings_store
from quant_research import overseas as overseas_mod
from quant_research import playbook as playbook_mod
from quant_research import review_archive as review_store
from quant_research import sector_flow as sector_flow_mod
from quant_research import sectors as sectors_mod
from quant_research import volume as volume_mod
from quant_research import watchlist as watchlist_store
from quant_research.backtest import (
    BacktestConfig,
    CostConfig,
    ExecutionConfig,
    PortfolioPolicy,
    RebalancePolicy,
)
from quant_research.backtest.engine import BacktestResult, run_signal_backtest
from quant_research.collector import collect_once
from quant_research.config import Settings, load_settings
from quant_research.data import PricePanel, describe_panel, load_price_panel, universe
from quant_research.data import etf as etf_mod
from quant_research.data import index as index_mod
from quant_research.data.cache import build_panel_cache, cache_path, load_panel_cache
from quant_research.data.calendar import TradingCalendar
from quant_research.data.providers import get_provider
from quant_research.data.providers.base import ProviderError, normalize_symbol
from quant_research.data.store import LocalMarketStore
from quant_research.fundamentals import fetch as fundamentals_fetch
from quant_research.fundamentals import risk as fundamentals_risk
from quant_research.indicators import (
    atr,
    compute_all,
    doc_for,
    family_of,
    load_bundle,
    params_from_settings,
    rsi,
    sma,
    store_bundle,
)
from quant_research.logging_setup import get_logger
from quant_research.market_events import detect_events
from quant_research.metrics import benchmark_buy_hold, drawdown_series
from quant_research.review import (
    STANCE_LABELS,
    STANCE_ORDER,
    ReviewRules,
    advise,
    discipline,
    summarize,
)
from quant_research.scoring import screen, to_target_weights
from quant_research.scoring.ledger import ScreenLedger, review
from quant_research.strategies import STRATEGY_REGISTRY, get_signal
from quant_research.strategies import custom as custom_mod
from quant_research.strategies import indicators as indicators_mod
from quant_research.volume import VolumeRules

from .experiments import ExperimentStore
from .experiments import get_store as get_experiments
from .grid import MAX_COMBINATIONS, expand, rank
from .jobs import JobStore, Progress, get_store
from .schemas import (
    AccountCreate,
    AccountRename,
    BacktestOut,
    BacktestRequest,
    CategoryIn,
    CollectRequest,
    CompareRequest,
    CompareRowOut,
    CostsIn,
    DatasetOut,
    ExperimentNoteIn,
    GridRequest,
    IndicatorBoardOut,
    IndicatorDetailOut,
    IndicatorRowOut,
    IndicatorSeriesOut,
    IndicatorValueOut,
    JobOut,
    JobRef,
    PolicyIn,
    PositionIn,
    PositionPatch,
    RebalanceIn,
    ReviewSaveIn,
    ScreenOut,
    ScreenPickOut,
    ScreenRequest,
    ScreenReviewOut,
    SeriesLineOut,
    SeriesPanelOut,
    SymbolOut,
    SymbolSearchOut,
    VariantIn,
    WatchlistIn,
)

logger = get_logger("web.api")

router = APIRouter(prefix="/api")


@router.get("/symbols", response_model=SymbolSearchOut)
def symbol_search(
    q: str = Query("", max_length=32),
    limit: int = Query(20, ge=1, le=50),
) -> SymbolSearchOut:
    """Autocomplete for the code inputs: match a code prefix or a name substring.

    Read-only and request-cheap: the table is loaded from disk, never fetched
    here, because the universe query costs tens of seconds and typists do not
    wait that long between keystrokes.
    """
    settings = load_settings()
    frame = universe.load(settings.data.directory)
    if frame.empty:
        return SymbolSearchOut(items=[], available=False, total=0)

    hits = universe.search(frame, q, limit=limit)
    stamp = frame["updated_at"].iloc[0] if "updated_at" in frame.columns else None
    return SymbolSearchOut(
        items=[SymbolOut(symbol=item.symbol, name=item.name) for item in hits],
        available=True,
        total=len(frame),
        updated_at=None if stamp is None else str(stamp)[:19],
    )


# --- datasets ----------------------------------------------------------------


def _discover_datasets(settings: Settings) -> dict[str, Path]:
    """Scan known locations. Only keys cross the wire, never arbitrary paths."""
    entries: dict[str, Path] = {}
    for path in sorted((settings.data.directory / "sample").glob("*.csv")):
        entries[f"sample:{path.stem}"] = path
    for daily in sorted(settings.collection.output_directory.glob("*/daily")):
        if not daily.is_dir():
            continue
        if any(daily.glob("*.csv")) or any(daily.glob("*.parquet")):
            entries[f"raw:{daily.parent.name}"] = daily
    return entries


def _resolve(key: str, settings: Settings) -> Path:
    datasets = _discover_datasets(settings)
    try:
        return datasets[key]
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"unknown dataset {key!r}; available: {sorted(datasets)}",
        ) from None


def _date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"expected YYYY-MM-DD, got {text!r}") from None


# --- serialisation -----------------------------------------------------------


def _points(series: pd.Series, max_points: int) -> list[list[Any]]:
    """Down-sample to an upper bound so a 10-year panel cannot swamp the browser."""
    values = series.dropna() if series.isna().any() else series
    if values.empty:
        return []
    if len(values) > max_points:
        step = math.ceil(len(values) / max_points)
        sampled = values.iloc[::step]
        if sampled.index[-1] != values.index[-1]:
            sampled = pd.concat([sampled, values.iloc[[-1]]])
        values = sampled
    return [
        [str(index.date()), None if pd.isna(value) else float(value)]
        for index, value in values.items()
    ]


def _metrics(series: pd.Series) -> dict[str, Any]:
    return {str(key): (None if pd.isna(value) else float(value)) for key, value in series.items()}


def _records(frame: pd.DataFrame, limit: int) -> tuple[list[dict[str, Any]], int]:
    if frame.empty:
        return [], 0
    head = frame.head(limit)
    return json.loads(head.to_json(orient="records", date_format="iso")), len(frame)


def _policy(model: PolicyIn | None) -> PortfolioPolicy | None:
    if model is None:
        return None
    return PortfolioPolicy(
        allow_short=model.allow_short,
        # "None" from the client means unconstrained, which is the policy default.
        max_weight=1.0 if model.max_weight is None else model.max_weight,
        max_gross=1.0 if model.max_gross is None else model.max_gross,
        target_net=model.target_net,
    )


# --- backtest ----------------------------------------------------------------


def _execute(request: BacktestRequest) -> tuple[BacktestResult, pd.Series]:
    """Run one backtest and return ``(result, benchmark_returns)``."""
    settings = load_settings()
    path = _resolve(request.dataset, settings)
    panel = load_price_panel(path, symbols=request.symbols, start=request.start, end=request.end)

    if request.strategy not in STRATEGY_REGISTRY:
        raise HTTPException(
            status_code=404,
            detail=f"unknown strategy {request.strategy!r}; available: {sorted(STRATEGY_REGISTRY)}",
        )
    factory = get_signal(request.strategy)
    kwargs: dict[str, Any] = dict(request.params)
    if "amount" in inspect.signature(factory).parameters:
        # 声明需要成交额的策略（放量/缩量条件）拿到面板实数；没有时策略内部如实降级
        kwargs.setdefault("amount", panel.field("amount") if panel.has_field("amount") else None)
    signal = factory(panel.prices, **kwargs)

    exec_prices = panel.field("open") if request.exec_price == "open" else None
    policy = _policy(request.policy)
    costs = request.costs or CostsIn()
    rebalance = request.rebalance or RebalanceIn()
    config = BacktestConfig.from_settings(
        settings,
        initial_cash=request.initial_cash,
        costs=CostConfig(
            commission_bps=costs.commission_bps,
            slippage_bps=costs.slippage_bps,
        ),
        execution=ExecutionConfig(price=request.exec_price),
        rebalance=RebalancePolicy(
            buffer=rebalance.buffer,
            min_trade_notional=rebalance.min_trade_notional,
        ),
        allow_short=policy.allow_short if policy else False,
    )

    risk = None
    if request.weight_method == "inverse_volatility":
        risk = (
            panel.prices.pct_change()
            .rolling(request.risk_window, min_periods=max(2, request.risk_window // 2))
            .std()
            .bfill()
        )

    result = run_signal_backtest(
        panel.prices,
        signal,
        weight_method=request.weight_method,
        target_exposure=request.target_exposure,
        risk=risk,
        policy=policy,
        exec_prices=exec_prices,
        config=config,
        meta=panel.meta,
    )

    return result, benchmark_buy_hold(panel.prices)


def _merge(base: BacktestRequest, variant: VariantIn) -> BacktestRequest:
    """Overlay the non-null fields of a variant onto the baseline."""
    payload = base.model_dump()
    for field in (
        "params",
        "weight_method",
        "exec_price",
        "costs",
        "rebalance",
        "policy",
        "target_exposure",
    ):
        value = getattr(variant, field)
        if value is not None:
            payload[field] = value
    return BacktestRequest(**payload)


# --- routes ------------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, Any]:
    settings = load_settings()
    return {
        "status": "ok",
        "strategies": sorted(STRATEGY_REGISTRY),
        "data_directory": str(settings.data.directory),
        "collection_root": str(settings.collection.output_directory),
    }


@router.get("/datasets", response_model=list[DatasetOut])
def datasets() -> list[DatasetOut]:
    settings = load_settings()
    items: list[DatasetOut] = []
    for key, path in _discover_datasets(settings).items():
        kind_raw, name = key.split(":", maxsplit=1)
        kind: Literal["sample", "collected"] = "sample" if kind_raw == "sample" else "collected"
        state = None
        if kind == "raw":
            state = LocalMarketStore(settings.collection.output_directory, name).load_state()
        items.append(
            DatasetOut(
                key=key,
                label=f"{name}（{kind}）",
                kind=kind,
                path=str(path),
                provider=name if kind == "raw" else None,
                n_symbols=None if state is None else len(state.symbols),
                updated_at=None if state is None else state.updated_at,
            )
        )
    return items


@router.get("/datasets/{key:path}/overview")
def overview(
    key: str,
    preview_rows: int = 60,
    max_points: int = 600,
    symbol_limit: int = 12,
) -> dict[str, Any]:
    settings = load_settings()
    path = _resolve(key, settings)
    panel = load_price_panel(path)
    quality = describe_panel(panel)

    base = panel.prices.bfill().iloc[0]
    normalised = panel.prices / base
    shown = list(panel.symbols)[:symbol_limit]
    series = {symbol: _points(normalised[symbol], max_points) for symbol in shown}

    preview = panel.prices.tail(preview_rows)
    rows = [
        [str(index.date()), *[None if pd.isna(value) else float(value) for value in row]]
        for index, row in zip(preview.index, preview.to_numpy(), strict=False)
    ]

    return {
        "dataset": key,
        "path": str(path),
        "meta": panel.meta.to_dict(),
        "quality": quality.to_dict(),
        "fields": ["close", *panel.extra],
        "series": series,
        "symbols_shown": shown,
        "truncated_symbols": len(panel.symbols) > symbol_limit,
        "preview": {"columns": list(preview.columns), "rows": rows},
    }


def _backtest_payload(request: BacktestRequest) -> BacktestOut:
    try:
        result, benchmark_returns = _execute(request)
    except HTTPException:
        raise
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    trades, trade_count = _records(result.trades, request.trade_limit)
    benchmark_equity = request.initial_cash * (1 + benchmark_returns).cumprod()
    return BacktestOut(
        dataset=request.dataset,
        meta=result.meta.to_dict() if result.meta else {},
        config=result.config.to_dict(),
        metrics=_metrics(result.summary(benchmark_returns)),
        equity=_points(result.equity, request.max_points),
        drawdown=_points(drawdown_series(result.returns), request.max_points),
        benchmark=_points(benchmark_equity, request.max_points),
        turnover=_points(result.turnover, request.max_points),
        trades=trades,
        trade_count=trade_count,
        final_equity=result.final_equity,
        total_costs=result.total_costs,
        mean_turnover=float(result.turnover.mean()) if len(result.turnover) else 0.0,
        warnings=[
            {"kind": str(kind), "symbol": str(symbol), "message": str(message)}
            for kind, symbol, message in result.warnings
        ],
        truncated=len(result.equity) > request.max_points or trade_count > request.trade_limit,
    )


def _describe(params: dict[str, Any], method: str | None = None) -> str:
    """Name a configuration — the label the ledger and the results table share."""
    text = " ".join(f"{key}={value}" for key, value in params.items())
    return f"{text} · {method}" if method else text


def _record_experiment(
    payload: BacktestOut, request: BacktestRequest, job_id: str | None, label: str = ""
) -> None:
    """Add a finished backtest to the ledger.

    A full ledger is worth more than a guaranteed one, but a full disk or a
    read-only directory must not fail a backtest that has already succeeded —
    so failures here are logged and swallowed.
    """
    try:
        get_experiments().record(
            dataset=request.dataset,
            strategy=request.strategy,
            metrics=payload.metrics,
            final_equity=payload.final_equity,
            trade_count=payload.trade_count,
            total_costs=payload.total_costs,
            params=request.params,
            weight_method=request.weight_method,
            rebalance=request.rebalance.model_dump() if request.rebalance else None,
            job_id=job_id,
            label=label,
        )
    except OSError as exc:
        logger.warning("could not record experiment for %s: %s", request.strategy, exc)


@router.post("/backtest", response_model=BacktestOut)
def backtest(request: BacktestRequest) -> BacktestOut:
    """Run inline. Fine for a small panel; use ``POST /jobs/backtest`` when slow."""
    payload = _backtest_payload(request)
    _record_experiment(payload, request, None)
    return payload


def _compare_rows(request: CompareRequest, progress: Progress | None = None) -> list[CompareRowOut]:
    runs: list[tuple[str, BacktestRequest]] = [("base", request.base)]
    runs.extend((variant.label, _merge(request.base, variant)) for variant in request.variants)

    rows: list[CompareRowOut] = []
    for index, (label, spec) in enumerate(runs):
        if progress is not None:
            progress.update(index, f"运行 {label}")
        try:
            result, benchmark_returns = _execute(spec)
        except HTTPException:
            raise
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{label}: {exc}") from exc

        rows.append(
            CompareRowOut(
                label=label,
                metrics=_metrics(result.summary(benchmark_returns)),
                equity=_points(result.equity, spec.max_points),
                final_equity=result.final_equity,
                total_costs=result.total_costs,
                trade_count=len(result.trades),
                mean_turnover=float(result.turnover.mean()) if len(result.turnover) else 0.0,
            )
        )
    if progress is not None:
        progress.update(len(runs), "完成")
    return rows


@router.post("/backtest/compare", response_model=list[CompareRowOut])
def compare(request: CompareRequest) -> list[CompareRowOut]:
    return _compare_rows(request)


@router.get("/collection/status")
def collection_status() -> dict[str, Any]:
    settings = load_settings()
    root = settings.collection.output_directory
    providers: list[dict[str, Any]] = []

    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            store = LocalMarketStore(root, child.name)
            state = store.load_state()
            providers.append(
                {
                    "provider": child.name,
                    "adjust": state.adjust,
                    "format": store.format,
                    "last_run_date": state.last_run_date,
                    "updated_at": state.updated_at,
                    "n_symbols": len(state.symbols),
                    "symbols": [
                        {"symbol": symbol, **info} for symbol, info in sorted(state.symbols.items())
                    ],
                    "failures": state.failures,
                }
            )

    return {"root": str(root), "providers": providers}


# A full-market sweep asks one host for thousands of symbols in a row, which is
# precisely what gets a connection closed on us. Pace it whether or not the
# caller thought to ask.
_MIN_SWEEP_PAUSE = 0.35


def _collect_report(
    request: CollectRequest,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    try:
        provider = get_provider(
            request.provider,
            adjust=settings.collection.adjust,
            **settings.collection.provider_options,
        )
    except (ValueError, ProviderError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = LocalMarketStore(
        settings.collection.output_directory,
        provider.name,
        fmt=settings.collection.format,
    )
    sweep = request.all_symbols and not request.symbols
    if sweep:
        if on_progress is not None:
            on_progress(0, 1, "拉取全市场清单…")
        try:
            symbols = provider.list_symbols()
        except ProviderError as exc:
            raise HTTPException(
                status_code=502, detail=f"{provider.name} 取全市场清单失败：{exc}"
            ) from exc
        # Listing the market takes seconds and cannot be interrupted from the
        # inside, so honour a cancellation as soon as it returns rather than
        # charging on into thousands of downloads the user already called off.
        cancelled = should_stop is not None and should_stop()
        if cancelled:
            symbols = []  # stop was called while listing: download nothing
        elif not symbols:
            raise HTTPException(status_code=502, detail=f"{provider.name} listed no symbols")
    else:
        symbols = request.symbols or store.stored_symbols()
        if not symbols:
            raise HTTPException(
                status_code=400,
                detail="没有指定标的，本地也还没有已存数据：填几只标的，或勾选全市场",
            )

    pause = max(request.pause, _MIN_SWEEP_PAUSE) if sweep else request.pause

    try:
        report = collect_once(
            provider=provider,
            store=store,
            symbols=symbols,
            start=_date(request.start),
            end=_date(request.end),
            full=request.full,
            lookback_days=(
                request.lookback_days
                if request.lookback_days is not None
                else settings.collection.lookback_days
            ),
            calendar=TradingCalendar.load_or_weekday(store.calendar_path),
            retries=request.retries,
            pause=pause,
            timezone=settings.collection.timezone,
            dry_run=request.dry_run,
            on_progress=on_progress,
            should_stop=should_stop,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "api triggered collection: %d updated, %d failed",
        len(report.updated),
        len(report.failed),
    )
    payload = report.to_dict()
    if sweep:
        # tell the caller what it actually bought: how many, and how slowly
        payload["swept"] = {"symbols": len(symbols), "pause": pause}
    return payload


@router.post("/collection/collect")
def collect(request: CollectRequest) -> dict[str, Any]:
    """Run inline; ``POST /jobs/collect`` for a long backfill."""
    return _collect_report(request)


@router.get("/collection/reports")
def collection_reports(limit: int = 20) -> dict[str, Any]:
    """Archived collection runs, newest first.

    The nightly run writes one JSON per day; without this the scheduler's output
    is only readable by opening files, which is how "did it work last night?"
    turns into guesswork.
    """
    settings = load_settings()
    root = settings.collection.report_directory
    reports: list[dict[str, Any]] = []

    if root.is_dir():
        for path in sorted(root.glob("*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                reports.append({"file": path.name, "unreadable": True})
                continue
            summary = data.get("summary") or {}
            reports.append(
                {
                    "file": path.name,
                    "provider": data.get("provider"),
                    "adjust": data.get("adjust"),
                    "started_at": data.get("started_at"),
                    "finished_at": data.get("finished_at"),
                    "requested": data.get("requested"),
                    "updated": summary.get("updated"),
                    "skipped": summary.get("skipped"),
                    "failed": summary.get("failed"),
                    "rows_added": summary.get("rows_added"),
                    "note": data.get("note"),
                    "failed_symbols": sorted((data.get("failed") or {}).keys()),
                }
            )

    return {"root": str(root), "reports": reports}


# --- jobs: long-running work outside the request thread ----------------------


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(jobs: Annotated[JobStore, Depends(get_store)], limit: int = 20) -> list[JobOut]:
    return [JobOut(**job.to_dict()) for job in jobs.list(limit)]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, jobs: Annotated[JobStore, Depends(get_store)]) -> JobOut:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id!r}")
    return JobOut(**job.to_dict(with_result=True))


@router.delete("/jobs/{job_id}")
def cancel_job(job_id: str, jobs: Annotated[JobStore, Depends(get_store)]) -> dict[str, Any]:
    if not jobs.cancel(job_id):
        raise HTTPException(status_code=404, detail=f"unknown or finished job {job_id!r}")
    return {"id": job_id, "status": "cancelling"}


@router.post("/jobs/backtest", response_model=JobRef, status_code=202)
def submit_backtest(
    request: BacktestRequest, jobs: Annotated[JobStore, Depends(get_store)]
) -> JobRef:
    def task(progress: Progress, stop: Callable[[], bool]) -> dict[str, Any]:
        progress.update(0, "加载面板")
        payload = _backtest_payload(request)
        # ``job`` is read only after the backtest finishes, by which time submit()
        # has returned and the name is bound.
        _record_experiment(payload, request, job.id)
        progress.update(1, "完成")
        return payload.model_dump()

    job = jobs.submit("backtest", f"{request.strategy} · {request.dataset}", task)
    return JobRef(id=job.id, kind=job.kind, label=job.label, status=job.status)


@router.post("/jobs/compare", response_model=JobRef, status_code=202)
def submit_compare(
    request: CompareRequest, jobs: Annotated[JobStore, Depends(get_store)]
) -> JobRef:
    total = 1 + len(request.variants)

    def task(progress: Progress, stop: Callable[[], bool]) -> list[dict[str, Any]]:
        return [row.model_dump() for row in _compare_rows(request, progress)]

    job = jobs.submit("compare", f"{total} 组 · {request.base.dataset}", task, total=total)
    return JobRef(id=job.id, kind=job.kind, label=job.label, status=job.status)


@router.post("/jobs/collect", response_model=JobRef, status_code=202)
def submit_collect(
    request: CollectRequest, jobs: Annotated[JobStore, Depends(get_store)]
) -> JobRef:
    settings = load_settings()
    try:
        get_provider(
            request.provider,
            adjust=settings.collection.adjust,
            **settings.collection.provider_options,
        )
    except (ValueError, ProviderError) as exc:
        # a source that cannot even be constructed (unknown name, missing SDK,
        # gateway not running) is the caller's problem to fix: 400 with the
        # reason, not a 500 with a stack trace
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    market = LocalMarketStore(
        settings.collection.output_directory, request.provider, fmt=settings.collection.format
    )
    sweep = request.all_symbols and not request.symbols
    if not sweep and not (request.symbols or market.stored_symbols()):
        raise HTTPException(
            status_code=400,
            detail="没有指定标的，本地也还没有已存数据：填几只标的，或勾选全市场",
        )

    def task(progress: Progress, stop: Callable[[], bool]) -> dict[str, Any]:
        def on_progress(done: int, total: int, symbol: str) -> None:
            # only the run itself knows how many symbols the source listed
            progress.set_total(total)
            progress.update(done, f"{symbol or '完成'}（{done}/{total}）")

        return _collect_report(request, on_progress=on_progress, should_stop=stop)

    label = (
        f"{request.provider} · 全市场"
        if sweep
        else f"{request.provider} · {len(request.symbols or market.stored_symbols())} 标的"
    )
    job = jobs.submit(
        "collect",
        label,
        task,
        total=1 if sweep else len(request.symbols or market.stored_symbols()),
    )
    return JobRef(id=job.id, kind=job.kind, label=job.label, status=job.status)


@router.post("/jobs/grid", response_model=JobRef, status_code=202)
def submit_grid(request: GridRequest, jobs: Annotated[JobStore, Depends(get_store)]) -> JobRef:
    """Sweep every combination, ranked by one metric.

    Each run goes through the same payload path as a single backtest and lands in
    the experiment ledger, so a sweep of two hundred leaves evidence behind
    instead of only a table on screen. Refuses oversized grids up front.
    """
    try:
        combos = expand(request.params)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not combos:
        raise HTTPException(status_code=422, detail="params is empty: nothing to sweep")

    methods = request.weight_methods or ["equal_weight"]
    runs = [(params, method) for params in combos for method in methods]
    if len(runs) > MAX_COMBINATIONS:
        raise HTTPException(
            status_code=422,
            detail=f"{len(runs)} runs (parameters × weight methods), limit is {MAX_COMBINATIONS}",
        )

    def task(progress: Progress, stop: Callable[[], bool]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        stopped = False

        for index, (params, method) in enumerate(runs, 1):
            if stop():
                stopped = True
                break
            # name the method only when it varies; otherwise every row would say
            # "equal_weight" and the parameters get harder to read
            label = _describe(params, method if len(methods) > 1 else None)
            progress.update(index - 1, label)
            single = BacktestRequest(
                dataset=request.dataset,
                strategy=request.strategy,
                params=params,
                weight_method=method,
                initial_cash=request.initial_cash,
                costs=request.costs,
                rebalance=request.rebalance,
            )
            payload = _backtest_payload(single)
            _record_experiment(payload, single, None, label=label)
            rows.append(
                {
                    "label": label,
                    "params": params,
                    "weight_method": method,
                    "metrics": payload.metrics,
                    "trade_count": payload.trade_count,
                    "total_costs": payload.total_costs,
                    "final_equity": payload.final_equity,
                }
            )
            progress.update(index, label)

        return {
            "metric": request.metric,
            "requested": len(runs),
            "completed": len(rows),
            "stopped": stopped,
            "rows": rank(rows, request.metric),
        }

    job = jobs.submit("grid", f"{request.strategy} · {len(runs)} 组", task, total=len(runs))
    return JobRef(id=job.id, kind=job.kind, label=job.label, status=job.status)


# --- experiments: the ledger of configurations already tried -----------------


# --- screening --------------------------------------------------------------


def _load_panel(provider: str, adjust: str):
    """The cached wide panel, built the first time it is asked for.

    Also rebuilt when the store holds files the panel does not know: a collection
    refreshes the cache itself, but a hand-copied file or an interrupted run can
    leave the two out of step, and the symptom is always the same — data on disk
    that no screen can see.
    """
    settings = load_settings()
    root = settings.data.directory
    if not cache_path(root, provider, adjust).is_file():
        logger.info("no cached panel for %s/%s; building one", provider, adjust)
        try:
            build_panel_cache(root, provider, adjust)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{provider} 还没有落盘数据（{exc}）；先采集，或换一个数据源",
            ) from exc
    panel = load_panel_cache(root, provider, adjust)
    if _store_outgrew_panel(root, provider, panel):
        logger.info("panel cache for %s/%s is behind the store; rebuilding", provider, adjust)
        try:
            build_panel_cache(root, provider, adjust)
        except (ValueError, FileNotFoundError):
            return panel  # serve what we have rather than fail a working request
        panel = load_panel_cache(root, provider, adjust)
    return panel


def _store_outgrew_panel(root: str | Path, provider: str, panel: PricePanel) -> bool:
    """True when the store holds more symbols than the cached panel does.

    One directory listing is cheaper than trusting every writer to have refreshed
    the cache, and a false negative merely delays the rebuild to the next run.
    """
    daily = Path(root).expanduser() / "raw" / provider / "daily"
    if not daily.is_dir():
        return False
    stored = sum(1 for path in daily.iterdir() if path.suffix in {".parquet", ".csv"})
    return stored > len(panel.symbols)


def _latest_session(provider: str, adjust: str) -> str | None:
    """The last day the cached panel knows about, or None when there is none."""
    settings = load_settings()
    root = settings.data.directory
    if not cache_path(root, provider, adjust).is_file():
        return None
    try:
        panel = load_panel_cache(root, provider, adjust)
    except (ValueError, FileNotFoundError):
        return None
    return pd.Timestamp(panel.prices.index.max()).date().isoformat()


def _screen_out(result: Any, provider: str, latest: Any, note: str = "") -> ScreenOut:
    held = to_target_weights(result).loc[result.as_of]
    held = held[held > 0]
    as_of = pd.Timestamp(result.as_of)
    latest_stamp = pd.Timestamp(latest)
    picks = [
        ScreenPickOut(
            symbol=str(symbol),
            rank=position,
            score=float(row["score"]),
            weight=float(held.get(symbol, 0.0)),
        )
        for position, (symbol, row) in enumerate(result.picks.iterrows(), start=1)
    ]
    return ScreenOut(
        as_of=as_of.date().isoformat(),
        latest_session=latest_stamp.date().isoformat(),
        provider=provider,
        universe=result.universe,
        picks=picks,
        dropped=dict(result.dropped),
        note=note,
        # a replayed list is about an older day; say so rather than letting the
        # panel imply it is today's opinion
        stale=as_of < latest_stamp,
    )


@router.get("/screen/latest", response_model=ScreenOut)
def screen_latest(provider: str | None = None, adjust: str | None = None) -> ScreenOut:
    """The most recent short list on file — what the panel shows when opened."""
    settings = load_settings()
    wanted_provider = provider or settings.collection.provider
    wanted_adjust = adjust or settings.collection.adjust
    records = ScreenLedger().recent()
    if not records:
        raise HTTPException(status_code=404, detail="no screening runs recorded yet")
    record = max(records, key=lambda item: item.as_of)

    # the panel is only needed to say how current the list is; without it the
    # list is still worth showing, dated by the day it was made
    latest = _latest_session(wanted_provider, wanted_adjust) or record.as_of
    ranked = sorted(record.picks.items(), key=lambda item: item[1], reverse=True)
    share = 1.0 / len(ranked) if ranked else 0.0
    return ScreenOut(
        as_of=record.as_of,
        latest_session=latest,
        provider=record.provider,
        universe=record.universe,
        picks=[
            ScreenPickOut(symbol=str(symbol), rank=position, score=float(score), weight=share)
            for position, (symbol, score) in enumerate(ranked, start=1)
        ],
        dropped=dict(record.dropped),
        note=record.note,
        stale=record.as_of < latest,
    )


@router.get("/screen/review", response_model=ScreenReviewOut)
def screen_review(
    horizon: int = 5, provider: str | None = None, adjust: str | None = None
) -> ScreenReviewOut:
    """How earlier short lists did, bought at the next open."""
    settings = load_settings()
    panel = _load_panel(
        provider or settings.collection.provider, adjust or settings.collection.adjust
    )
    frame = review(ScreenLedger(), panel, horizon=horizon)
    if frame.empty:
        return ScreenReviewOut(horizon=horizon, rows=[])

    rows: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        rows.append(
            {
                key: value.isoformat() if isinstance(value, (pd.Timestamp, datetime)) else value
                for key, value in row.items()
            }
        )
    return ScreenReviewOut(
        horizon=horizon,
        rows=rows,
        screens=len(frame),
        mean_excess=float(frame["excess"].mean()),
        beat_market=float(frame["beat_market"].mean()),
    )


@router.post("/jobs/screen", response_model=JobRef, status_code=202)
def submit_screen(request: ScreenRequest, jobs: Annotated[JobStore, Depends(get_store)]) -> JobRef:
    """Rank the market in the background; a replay is not written to the ledger."""
    settings = load_settings()
    provider = request.provider or settings.collection.provider
    adjust = request.adjust or settings.collection.adjust

    def task(progress: Progress, stop: Callable[[], bool]) -> dict[str, Any]:
        progress.set_total(3)
        progress.update(0, "读取面板…")
        panel = _load_panel(provider, adjust)
        if stop():
            return {}
        progress.update(1, "打分…")
        result = screen(
            panel,
            as_of=request.as_of,
            top_n=request.top_n,
            min_history=request.min_history,
            min_amount=request.min_amount,
        )
        progress.update(2, "归档…")
        # a replay is a question about the past, not an opinion held that day
        if request.record and request.as_of is None:
            ScreenLedger().record(
                result,
                provider=provider,
                adjust=adjust,
                params={
                    "top_n": request.top_n,
                    "min_history": request.min_history,
                    "min_amount": request.min_amount,
                },
                note=request.note,
            )
        return _screen_out(
            result, provider, panel.prices.index.max(), note=request.note
        ).model_dump()

    label = f"{provider} · Top {request.top_n}"
    if request.as_of:
        label = f"{label} · 回放 {request.as_of}"
    job = jobs.submit("screen", label, task, total=3)
    return JobRef(id=job.id, kind=job.kind, label=job.label, status=job.status)


# --- indicators --------------------------------------------------------------


def _cache_is_stale(bundle: Any, provider: str, adjust: str) -> bool:
    """A stored bundle goes obsolete when names appear, a session closes, or the
    configured periods change.

    Recomputing here rather than answering 404 with "run quant-indicators" — the
    user just collected the stock they want to look at, and the next click should
    show it. Rebuilding is what a cache miss is *for*; making a human remember a
    command is just moving the work.
    """
    try:
        panel = _load_panel(provider, adjust)
    except HTTPException:
        return False  # nothing to compare against; serve what is stored
    if bundle.params != params_from_settings(load_settings()):
        return True  # someone edited the periods in settings.yaml
    cached = next(iter(bundle.frames.values()))
    if not set(panel.prices.columns) <= set(cached.columns):
        return True
    latest = panel.prices.index.max()
    return bool(pd.notna(latest) and cached.index.max() < latest)


def _load_indicators(provider: str, adjust: str, rebuild: bool = False) -> Any:
    """The stored indicator bundle, computed from the cached panel on first ask.

    Reading first and computing second keeps one source of truth: the numbers on
    screen are the ones ``quant-indicators`` would have written, not a second
    implementation that happens to live in the web layer.
    """
    settings = load_settings()
    root = settings.indicators.output_directory
    if not rebuild:
        stored = load_bundle(root, provider, adjust)
        if stored is not None and not _cache_is_stale(stored, provider, adjust):
            return stored

    panel = _load_panel(provider, adjust)
    bundle = compute_all(panel, params_from_settings(settings))
    store_bundle(bundle, root, provider, adjust, fmt=settings.indicators.format)
    logger.info("computed %d indicators for %s/%s", len(bundle.frames), provider, adjust)
    return bundle


def _labelled(field: str, doc: Any) -> str:
    """``ma_20`` must not read as「MA（均线）」— the period is part of the name.

    An interface that says only "RSI" leaves the reader unable to tell 14 from 6,
    and the threshold they remember belongs to one of them.
    """
    digits = field[len(field.rstrip("0123456789")) :]
    return f"{doc.label}{digits}" if digits else doc.label


@router.get("/indicators", response_model=IndicatorBoardOut)
def indicator_board(
    provider: str | None = None, adjust: str | None = None, rebuild: bool = False
) -> IndicatorBoardOut:
    """Every indicator with its last cross-section and its explanation."""
    settings = load_settings()
    wanted_provider = provider or settings.collection.provider
    wanted_adjust = adjust or settings.collection.adjust
    bundle = _load_indicators(wanted_provider, wanted_adjust, rebuild=rebuild)

    sample = next(iter(bundle.frames.values()))
    as_of = pd.Timestamp(sample.index.max())
    rows: list[IndicatorRowOut] = []
    for field, frame in bundle.frames.items():
        doc = doc_for(field)
        if doc is None:
            # a number nobody can explain is worse than no number at all
            logger.warning("indicator %s has no catalogue entry; leaving it out", field)
            continue
        latest = (frame.iloc[-1] if not frame.empty else pd.Series(dtype="float64")).dropna()
        present = len(latest)
        rows.append(
            IndicatorRowOut(
                field=field,
                label=_labelled(field, doc),
                unit=doc.unit,
                typical_range=doc.typical_range,
                description=doc.description,
                how_to_use=doc.how_to_use,
                caution=doc.caution,
                median=float(latest.median()) if present else None,
                low=float(latest.min()) if present else None,
                high=float(latest.max()) if present else None,
                symbols=int(present),
            )
        )

    latest_session = _latest_session(wanted_provider, wanted_adjust)
    return IndicatorBoardOut(
        as_of=as_of.date().isoformat(),
        provider=wanted_provider,
        adjust=wanted_adjust,
        symbols=int(sample.shape[1]),
        fields=len(bundle.frames),
        params=bundle.params.to_dict(),
        rows=rows,
        stale=latest_session is not None and as_of < pd.Timestamp(latest_session),
    )


def _stored_sources(symbol: str, settings: Settings) -> list[str]:
    """Which stored sources actually hold this code, e.g. ``['akshare']``.

    The point of the check is a one-line answer to "then where is it?": the code
    is in the store, just under another source's folder.
    """
    root = Path(settings.collection.output_directory).expanduser()
    if not root.is_dir():
        return []
    return sorted({path.parent.parent.name for path in root.glob(f"*/daily/{symbol}.*")})


def _canonical_symbol(raw: str, sample: pd.DataFrame, settings: Settings, provider: str) -> str:
    """Whatever was typed, reduced to a code the indicator set can answer for.

    ``600519`` and even ``600519.HZ`` are the same stock as ``600519.SH`` — the
    exchange is inferable from the digits, so a typo'd suffix deserves forgiving,
    not "not in the set". When the code truly is missing, the 404 names the stock
    and the closest match: ``600519.HZ 不在指标集合里`` reads like the set is
    broken, when the input is what is broken.
    """
    try:
        wanted = normalize_symbol(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"无法识别的代码 {raw!r}：{exc}") from exc
    if wanted in sample.columns:
        return wanted

    # the digits fix the exchange, so a normalised code either is in the set or
    # has no same-digit cousin in it — "did you mean" has nothing to point at
    name = ""
    table = universe.load(settings.data.directory)
    if not table.empty:
        hit = table.loc[table["symbol"] == wanted, "name"]
        if len(hit):
            name = str(hit.iloc[0]).strip()

    detail = f"{wanted}{f' {name}' if name else ''} 不在指标集合里（共 {sample.shape[1]} 只）"
    elsewhere = [source for source in _stored_sources(wanted, settings) if source != provider]
    if elsewhere:
        detail += f"；{provider} 源里没有它的行情，{'、'.join(elsewhere)} 源里有——切换数据源即可"
    else:
        detail += f"；先采集：quant-collect --provider {provider} --symbols {wanted}"
    raise HTTPException(status_code=404, detail=detail)


@router.get("/indicators/{symbol}", response_model=IndicatorDetailOut)
def indicator_detail(
    symbol: str, provider: str | None = None, adjust: str | None = None
) -> IndicatorDetailOut:
    """One name's indicator values — what the cross-section was made of."""
    settings = load_settings()
    wanted_provider = provider or settings.collection.provider
    wanted_adjust = adjust or settings.collection.adjust
    bundle = _load_indicators(wanted_provider, wanted_adjust)

    sample = next(iter(bundle.frames.values()))
    symbol = _canonical_symbol(symbol, sample, settings, wanted_provider)

    values: list[IndicatorValueOut] = []
    for field, frame in bundle.frames.items():
        doc = doc_for(field)
        if doc is None:
            continue
        raw = frame[symbol].iloc[-1]
        values.append(
            IndicatorValueOut(
                field=field,
                label=_labelled(field, doc),
                unit=doc.unit,
                value=None if pd.isna(raw) else float(raw),
            )
        )
    sector_map, _ = _sector_map(settings)
    return IndicatorDetailOut(
        symbol=symbol,
        as_of=pd.Timestamp(sample.index.max()).date().isoformat(),
        provider=wanted_provider,
        sector=sector_map.get(symbol),
        values=values,
    )


# Chart panels, described by indicator *family* rather than by field name: with
# ``rsi_period: 21`` the line still appears instead of the chart going empty.
_PANELS: tuple[tuple[str, str, str, tuple[str, ...], tuple[float, ...]], ...] = (
    # the price panel's overlays are assembled dynamically (bollinger + whatever
    # MA periods the settings asked for), so its family tuple stays empty here
    ("price", "K线 · 均线 · 布林带", "价格", (), ()),
    ("rsi", "RSI", "0–100", ("rsi",), (20.0, 80.0)),
    ("macd", "MACD", "价格", ("macd_dif", "macd_dea", "macd_hist"), ()),
    ("kdj", "KDJ", "0–100", ("kdj_k", "kdj_d", "kdj_j"), (20.0, 80.0)),
)


def _indicator_points(series: pd.Series, days: int) -> list[list[Any]]:
    """``[[date, value]]`` with NaN as null: a warm-up gap must stay a gap.

    Rounding to four decimals keeps a 500-day response small without changing
    anything a chart can show.
    """
    tail = series.tail(days)
    return [
        [stamp.date().isoformat(), None if pd.isna(value) else round(float(value), 4)]
        for stamp, value in tail.items()
    ]


def _candles(panel: PricePanel, symbol: str, days: int) -> list[list[Any]]:
    """``[date, open, high, low, close]`` for the candlestick chart.

    A bar whose OHLC is incomplete (a suspended session, a missing field) is
    dropped rather than drawn as a stub: a candle with one leg missing reads as
    data, and it would be the wrong data.
    """
    opens = panel.extra.get("open", panel.prices)[symbol]
    highs = panel.extra.get("high", panel.prices)[symbol]
    lows = panel.extra.get("low", panel.prices)[symbol]
    closes = panel.prices[symbol]
    out: list[list[Any]] = []
    for stamp in closes.index[-days:]:
        values = [opens[stamp], highs[stamp], lows[stamp], closes[stamp]]
        if any(pd.isna(value) for value in values):
            continue
        o, h, l, c = (round(float(value), 4) for value in values)
        out.append([pd.Timestamp(stamp).date().isoformat(), o, h, l, c])
    return out


@router.get("/indicators/{symbol}/series", response_model=IndicatorSeriesOut)
def indicator_series(
    symbol: str,
    provider: str | None = None,
    adjust: str | None = None,
    days: int = Query(120, ge=5, le=500),
) -> IndicatorSeriesOut:
    """A name's indicator history, split into panels a chart can draw alone.

    Split here rather than in the browser: RSI and MACD share no scale, and a
    single chart with two y-axes invites reading crossings that mean nothing.
    """
    settings = load_settings()
    wanted_provider = provider or settings.collection.provider
    wanted_adjust = adjust or settings.collection.adjust
    bundle = _load_indicators(wanted_provider, wanted_adjust)

    sample = next(iter(bundle.frames.values()))
    symbol = _canonical_symbol(symbol, sample, settings, wanted_provider)

    by_family: dict[str, str] = {}
    for field in bundle.frames:
        by_family.setdefault(family_of(field), field)

    panels: list[SeriesPanelOut] = []
    for key, title, unit, families, reference in _PANELS:
        lines: list[SeriesLineOut] = []
        candles: list[list[Any]] = []
        if key == "price":
            # the candle already carries the close, so no separate close line;
            # the overlays (bollinger + every configured MA) go on top of it
            panel = _load_panel(wanted_provider, wanted_adjust)
            candles = _candles(panel, symbol, days)
            families = ("boll_mid", "boll_upper", "boll_lower") + tuple(
                field for field in bundle.frames if field.startswith("ma_")
            )
        for family in families:
            # a family name ("rsi") picks the first field of that family; an
            # explicit field name ("ma_5") must survive the lookup untouched,
            # because the price panel pins specific moving averages
            field = by_family.get(family, family if family in bundle.frames else None)
            if field is None:
                continue
            doc = doc_for(field)
            lines.append(
                SeriesLineOut(
                    field=field,
                    label=_labelled(field, doc) if doc is not None else field,
                    # the histogram is about which side of zero, not how it slopes
                    kind="bar" if family.endswith("_hist") else "line",
                    points=_indicator_points(bundle.frames[field][symbol], days),
                )
            )
        if lines or candles:
            panels.append(
                SeriesPanelOut(
                    key=key,
                    title=title,
                    unit=unit,
                    reference=list(reference),
                    lines=lines,
                    candles=candles,
                )
            )

    return IndicatorSeriesOut(
        symbol=symbol,
        as_of=pd.Timestamp(sample.index.max()).date().isoformat(),
        provider=wanted_provider,
        days=days,
        panels=panels,
    )


@router.get("/experiments")
def list_experiments(
    experiments: Annotated[ExperimentStore, Depends(get_experiments)],
    dataset: str | None = None,
    strategy: str | None = None,
    sort: str = "created_at",
    limit: int = 50,
) -> dict[str, Any]:
    """Every backtest that has been run, newest first or best first.

    ``sort`` takes a metric name (``sharpe``, ``cagr``, ``max_drawdown`` …);
    runs where that metric could not be computed sink to the bottom rather than
    masquerading as zero.
    """
    items = experiments.recent(dataset=dataset, strategy=strategy, sort=sort, limit=limit)
    return {
        "path": str(experiments.path or ""),
        "sort": sort,
        "experiments": [item.to_dict() for item in items],
    }


@router.patch("/experiments/{experiment_id}")
def update_experiment(
    experiment_id: str,
    body: ExperimentNoteIn,
    experiments: Annotated[ExperimentStore, Depends(get_experiments)],
) -> dict[str, Any]:
    """Attach a label or a note — the reason a run is worth remembering."""
    updated = experiments.update(experiment_id, label=body.label, note=body.note)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"unknown experiment {experiment_id!r}")
    return updated.to_dict()


@router.delete("/experiments/{experiment_id}")
def delete_experiment(
    experiment_id: str,
    experiments: Annotated[ExperimentStore, Depends(get_experiments)],
) -> dict[str, Any]:
    if not experiments.delete(experiment_id):
        raise HTTPException(status_code=404, detail=f"unknown experiment {experiment_id!r}")
    return {"id": experiment_id, "deleted": True}


__all__ = ["router"]


# --- watchlist ---------------------------------------------------------------


def _quote(panel: PricePanel | None, symbol: str) -> dict[str, Any]:
    """Last close and the day-over-day change, read live from the panel.

    The watchlist never stores prices: a stored quote is a stale quote, and
    reading the panel costs milliseconds for the handful of names on it.
    """
    if panel is None or symbol not in panel.prices.columns:
        return {"last_close": None, "change_pct": None}
    close = panel.prices[symbol].dropna()
    if len(close) < 2 or not close.iloc[-1]:
        return {"last_close": None, "change_pct": None}
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    change = round((last / prev - 1) * 100, 2) if prev else None
    return {"last_close": last, "change_pct": change}


def _watchlist_payload(settings: Settings) -> dict[str, Any]:
    data = watchlist_store.load(settings.data.directory)
    try:
        panel = _load_panel(settings.collection.provider, settings.collection.adjust)
    except HTTPException:
        panel = None  # no data yet: the list still works, quotes just stay blank
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})
    sector_map, _ = _sector_map(settings)

    stocks = []
    for symbol, entry in sorted(data["stocks"].items()):
        stocks.append(
            {
                "symbol": symbol,
                "name": entry.get("name") or name_of.get(symbol, ""),
                "sector": sector_map.get(symbol),
                "categories": entry.get("categories", []),
                "note": entry.get("note", ""),
                "added_at": entry.get("added_at"),
                **_quote(panel, symbol),
            }
        )
    return {"categories": data["categories"], "stocks": stocks}


@router.get("/watchlist")
def get_watchlist() -> dict[str, Any]:
    """The user's own list: groups first, quotes read live from the panel."""
    return _watchlist_payload(load_settings())


@router.post("/watchlist")
def add_to_watchlist(request: WatchlistIn) -> dict[str, Any]:
    settings = load_settings()
    watchlist_store.add_stock(
        settings.data.directory,
        request.symbol,
        name=request.name,
        categories=request.categories,
        note=request.note,
    )
    return _watchlist_payload(settings)


@router.delete("/watchlist/{symbol}")
def drop_from_watchlist(symbol: str) -> dict[str, Any]:
    settings = load_settings()
    watchlist_store.remove_stock(settings.data.directory, symbol)
    return _watchlist_payload(settings)


@router.post("/watchlist/categories")
def new_category(request: CategoryIn) -> dict[str, Any]:
    settings = load_settings()
    return _watchlist_payload_set(
        settings, watchlist_store.upsert_category(settings.data.directory, request.name)
    )


@router.delete("/watchlist/categories/{name}")
def drop_category(name: str) -> dict[str, Any]:
    """Removing a group keeps the stocks — they just lose the tag."""
    settings = load_settings()
    return _watchlist_payload_set(
        settings, watchlist_store.remove_category(settings.data.directory, name)
    )


def _watchlist_payload_set(settings: Settings, data: dict[str, Any]) -> dict[str, Any]:
    """Same shape as ``_watchlist_payload`` but from data already in hand."""
    try:
        panel = _load_panel(settings.collection.provider, settings.collection.adjust)
    except HTTPException:
        panel = None
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})
    sector_map, _ = _sector_map(settings)
    stocks = [
        {
            "symbol": symbol,
            "name": entry.get("name") or name_of.get(symbol, ""),
            "sector": sector_map.get(symbol),
            "categories": entry.get("categories", []),
            "note": entry.get("note", ""),
            "added_at": entry.get("added_at"),
            **_quote(panel, symbol),
        }
        for symbol, entry in sorted(data["stocks"].items())
    ]
    return {"categories": data["categories"], "stocks": stocks}


# --- portfolio: positions the user actually holds --------------------------


@router.get("/portfolio")
def get_portfolio() -> dict[str, Any]:
    """持仓清单：成本与股数是用户录的，现价与盈亏从面板现读。"""
    settings = load_settings()
    return _portfolio_payload(settings)


@router.post("/portfolio")
def upsert_position(request: PositionIn) -> dict[str, Any]:
    settings = load_settings()
    try:
        holdings_store.upsert(
            settings.data.directory,
            request.symbol,
            shares=request.shares,
            cost=request.cost,
            note=request.note,
            account=request.account,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _portfolio_payload(settings)


@router.patch("/portfolio/{symbol}")
def patch_position(
    symbol: str, request: PositionPatch, account: str | None = None
) -> dict[str, Any]:
    """改一条持仓：股数 / 成本 / 备注 / 挪账户，一次原子落盘。

    account 是定位用的旧账户（该票多账户都有时必填）；to_account 是
    挪过去的新账户。任何字段都不传等于没改，但也会走一次落盘——无害。
    """
    settings = load_settings()
    try:
        holdings_store.update(
            settings.data.directory,
            symbol,
            account=account,
            shares=request.shares,
            cost=request.cost,
            note=request.note,
            to_account=request.to_account,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _portfolio_payload(settings)


@router.post("/portfolio/accounts")
def add_account(request: AccountCreate) -> dict[str, Any]:
    """新建账户（可空户）：先建户、后录入，录入时只选不打字。

    重名拒绝——两个「华泰」会让「卖哪个户的货」变成猜谜。
    """
    settings = load_settings()
    try:
        holdings_store.create_account(settings.data.directory, request.name, note=request.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _portfolio_payload(settings)


@router.post("/portfolio/accounts/rename")
def rename_account(request: AccountRename) -> dict[str, Any]:
    """重命名账户：持仓整体迁移（added_at 保留），复盘存档键同步迁移。

    存档键带账户——账本改名存档不改，「和上次比什么变了」就比不上。
    有重叠持仓时 400 列出重叠代码，拒绝合并。
    """
    settings = load_settings()
    root = settings.data.directory
    try:
        holdings_store.rename_account(root, request.old, request.new)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    review_store.rename_account(root, request.old, request.new)
    return _portfolio_payload(settings)


@router.delete("/portfolio/accounts/{name}")
def drop_account(name: str) -> dict[str, Any]:
    """删除账户：该账户下所有持仓一并移除。

    存档刻意保留：复盘存的是「那天我看到的结论」，不是持仓的附属——
    账户删了，历史档位仍在，失去匹配对象的条目不进对比。
    """
    settings = load_settings()
    try:
        holdings_store.remove_account(settings.data.directory, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _portfolio_payload(settings)


@router.delete("/portfolio/{symbol}")
def drop_position(symbol: str, account: str | None = None) -> dict[str, Any]:
    settings = load_settings()
    try:
        holdings_store.remove(settings.data.directory, symbol, account=account)
    except ValueError as exc:
        # 同一票多账户都有：要求指定账户，不一次删两笔
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _portfolio_payload(settings)


@router.post("/portfolio/backfill")
def portfolio_backfill() -> dict[str, Any]:
    """一键补齐持仓 ∪ 自选里缺数据的票。

    缺什么补什么：股票缺面板数据走增量采集（落盘后进面板），ETF 缺
    落盘走 ETF 采集（ETF 独立于股票面板，不拿面板判缺）。已有数据的
    不碰；单只失败不拖垮整批，逐只报告——「补齐了什么」和「还差什么」
    都看得见。
    """
    settings = load_settings()
    root = settings.data.directory
    watch = watchlist_store.load(root).get("stocks", {})
    book = sorted({*holdings_store.symbols(holdings_store.load(root)), *watch})
    if not book:
        raise HTTPException(status_code=400, detail="持仓与自选都是空的——先录入再补采")

    panel = _load_panel(settings.collection.provider, settings.collection.adjust)

    def is_missing(symbol: str) -> bool:
        if etf_mod.is_etf(symbol):
            return etf_mod.load(root, symbol) is None
        return symbol not in panel.prices.columns

    todo = [symbol for symbol in book if is_missing(symbol)]
    skipped = [symbol for symbol in book if not is_missing(symbol)]
    if not todo:
        return {
            "requested": book,
            "updated": [],
            "failed": [],
            "skipped": skipped,
            "note": "持仓与自选的数据都齐了，没有要补的",
        }

    updated: dict[str, int] = {}
    failed: dict[str, str] = {}
    stocks = [symbol for symbol in todo if not etf_mod.is_etf(symbol)]
    etfs = [symbol for symbol in todo if etf_mod.is_etf(symbol)]
    if stocks:
        try:
            report = _collect_report(
                CollectRequest(provider=settings.collection.provider, symbols=stocks)
            )
            updated.update(report.get("updated") or {})
            failed.update(report.get("failed") or {})
        except HTTPException as exc:
            # 整批都失败（如 provider 挂了）：如实整批报告
            failed.update({symbol: exc.detail for symbol in stocks})
    for bare in etfs:
        try:
            frame = etf_mod.refresh(root, bare, adjust=settings.collection.adjust)
            updated[bare] = len(frame)
        except Exception as exc:  # noqa: BLE001 - 单只失败逐条上报
            failed[bare] = str(exc)

    note = (
        f"补采完成：成功 {len(updated)}、失败 {len(failed)}"
        if failed
        else f"补采完成：{len(updated)} 只入库"
    )
    return {
        "requested": book,
        "updated": updated,
        "failed": failed,
        "skipped": skipped,
        "note": note,
    }


@router.get("/portfolio/review")
def portfolio_review(provider: str | None = None, adjust: str | None = None) -> dict[str, Any]:
    """持仓复盘：每只票一档结论 + 情景预案 + 明日关注价位，外加纪律检查。

    结论是描述性的（「亏损超阈值且跌破 MA20」），不是买卖指令：
    仓位大小、资金性质、你的判断不在这里，也不该假装在。
    响应里的 ``previous``/``changes`` 来自上一次存档——复盘的本质是对比。
    """
    settings = load_settings()
    source = provider or settings.collection.provider
    basis = adjust or settings.collection.adjust
    payload = _review_payload(settings, source, basis)
    payload["previous"] = _previous_review(settings, payload["as_of"])
    payload["changes"] = _changes(payload["positions"], payload["previous"])
    return payload


@router.post("/portfolio/review/save")
def save_portfolio_review(request: ReviewSaveIn | None = None) -> dict[str, Any]:
    """把当前复盘存为今日存档——显式动作，GET 不写盘。

    存档是「那天我看到的档位」，下次打开时回答「和上次比，什么变了」。
    请求体可选：note 是用户当天手写的备注（为什么改档位、明天打算怎么
    操作），随存档一起落盘——档位是机器算的，备注是人写的，都得留下。
    """
    settings = load_settings()
    payload = _review_payload(settings, settings.collection.provider, settings.collection.adjust)
    if not payload["as_of"]:
        raise HTTPException(
            status_code=400,
            detail=payload["unavailable"] or "还没有持仓可以复盘",
        )
    # 备注语义：省略 = 继承当天已有的备注（重复存档不该把手写的注解
    # 抹掉）；显式空串 = 清除。档位每次现算，备注只属于「人写的」。
    root = settings.data.directory
    note = request.note if request is not None else None
    if note is None:
        existing = review_store.load(root, payload["as_of"])
        note = (existing or {}).get("user_note")
    payload["user_note"] = note
    previous = review_store.latest(root, exclude=payload["as_of"])
    path = review_store.save(settings.data.directory, payload)
    return {
        "saved": path.name,
        "date": payload["as_of"],
        "changes": _changes(payload["positions"], previous),
    }


@router.get("/portfolio/review/history")
def review_history() -> dict[str, Any]:
    """历次复盘存档的清单（新到旧）：备注、结论、档位概况一眼可见。

    只读目录不联网——存的是「那天看到的」，不是行情；重算才需要面板。
    """
    root = load_settings().data.directory
    folder = review_store.directory(root)
    if not folder.is_dir():
        return {"history": []}
    history: list[dict[str, Any]] = []
    for file in sorted(folder.glob("*.json"), reverse=True):
        data = review_store.load(root, file.stem)
        if not data:
            continue
        positions = data.get("positions") or {}
        history.append(
            {
                "date": data.get("date") or file.stem,
                "saved_at": data.get("saved_at"),
                "user_note": data.get("user_note"),
                "summary_note": data.get("summary_note"),
                "n_positions": len(positions),
                "n_alerts": sum(
                    1
                    for entry in positions.values()
                    if entry.get("stance") and entry["stance"] != "hold"
                ),
            }
        )
    return {"history": history}


@router.get("/portfolio/review/archive/{date}")
def review_archive_detail(date: str) -> dict[str, Any]:
    """单日存档详情：档位逐条 + 用户备注 + 与再上一次的档位变化。

    存档日期不存在是 404 指路（先存档）；对比对象是这份存档之前最近
    的一份——「那天和那天前比」，不是「那天和今天比」。
    """
    root = load_settings().data.directory
    data = review_store.load(root, date)
    if not data:
        raise HTTPException(
            status_code=404, detail=f"{date} 没有复盘存档：先在该日点「存为今日复盘」"
        )
    previous = review_store.latest(root, exclude=date)
    positions = data.get("positions") or {}
    rows = [
        {
            "symbol": entry.get("symbol"),
            "account": entry.get("account") or holdings_store.DEFAULT_ACCOUNT,
            "label": entry.get("label") or entry.get("stance"),
            "stance": entry.get("stance"),
            "close": entry.get("close"),
            "pnl_pct": entry.get("pnl_pct"),
        }
        for entry in positions.values()
    ]
    return {
        "date": data.get("date"),
        "saved_at": data.get("saved_at"),
        "user_note": data.get("user_note"),
        "summary_note": data.get("summary_note"),
        "positions": rows,
        "changes": _changes(rows, previous),
    }


def _review_payload(settings: Settings, source: str, basis: str) -> dict[str, Any]:
    rules = ReviewRules(
        stop_loss_pct=settings.review.stop_loss_pct,
        take_profit_pct=settings.review.take_profit_pct,
        overbought_rsi=settings.review.overbought_rsi,
        extreme_rsi=settings.review.extreme_rsi,
        near_high_pct=settings.review.near_high_pct,
        atr_stop_multiple=settings.review.atr_stop_multiple,
        max_single_pct=settings.review.max_single_pct,
        max_risk_ratio_pct=settings.review.max_risk_ratio_pct,
    )
    data = holdings_store.load(settings.data.directory)
    if not data["positions"]:
        return {
            "as_of": None,
            "provider": source,
            "summary": None,
            "positions": [],
            "discipline": [],
            "unavailable": None,
        }

    try:
        panel = _load_panel(source, basis)
    except HTTPException as exc:
        return {
            "as_of": None,
            "provider": source,
            "summary": None,
            "positions": [],
            "discipline": [],
            "unavailable": str(exc.detail),
        }

    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})
    sector_map, _ = _sector_map(settings)
    as_of = pd.Timestamp(panel.prices.index.max()).date().isoformat()

    rows: list[dict[str, Any]] = []
    for entry in data["positions"].values():
        symbol = str(entry.get("symbol"))
        account = str(entry.get("account") or holdings_store.DEFAULT_ACCOUNT)
        shares = float(entry.get("shares") or 0)
        cost = float(entry.get("cost") or 0)
        state = _position_state(panel, symbol)
        verdict = advise(
            close=state["close"],
            cost=cost,
            rsi=state["rsi_14"],
            ma20=state["ma20"],
            ma60=state["ma60"],
            atr=state["atr_14"],
            high20=state["high20"],
            low20=state["low20"],
            rules=rules,
        )
        value = None if state["close"] is None else round(shares * state["close"], 2)
        pnl = None if value is None else round(value - shares * cost, 2)
        rows.append(
            {
                "symbol": symbol,
                "account": account,
                "name": name_of.get(symbol, ""),
                "sector": sector_map.get(symbol),
                "shares": shares,
                "cost": cost,
                "note": entry.get("note", ""),
                **state,
                "market_value": value,
                "pnl": pnl,
                "pnl_pct": None
                if pnl is None or not shares * cost
                else round(pnl / (shares * cost) * 100, 2),
                **verdict.to_dict(),
            }
        )

    rows.sort(
        key=lambda row: (
            STANCE_ORDER.get(row["stance"], 9),
            row["account"] != holdings_store.DEFAULT_ACCOUNT,
            row["account"],
            row["symbol"],
        )
    )
    scoped = [
        {
            "symbol": row["symbol"],
            "account": row["account"],
            "stance": row["stance"],
            "shares": row["shares"],
            "cost": row["cost"],
            "close": row["close"],
        }
        for row in rows
    ]
    summary = summarize(scoped, rules)
    return {
        "as_of": as_of,
        "provider": source,
        "summary": summary,
        "discipline": discipline(scoped, summary, rules),
        "positions": rows,
        "unavailable": None,
    }


def _previous_review(settings: Settings, as_of: str | None) -> dict[str, Any] | None:
    """上一次存档：优先跳过当天；只剩当天一份时，它就是「最近一次存档」。

    刚存完档就刷新页面的情形：「还没有历史存档」会让人以为保存失败了。
    """
    previous = review_store.latest(settings.data.directory, exclude=as_of)
    if previous is None and as_of:
        previous = review_store.latest(settings.data.directory)
    return previous


def _changes(rows: list[dict[str, Any]], previous: dict[str, Any] | None) -> list[dict[str, Any]]:
    """档位与上次不同才有条目：复盘的价值在「什么变了」。"""
    archived = (previous or {}).get("positions") or {}
    changes = []
    for row in rows:
        account = str(row.get("account") or holdings_store.DEFAULT_ACCOUNT)
        # 新存档键是「账户::代码」；旧存档只有代码——两级回退，历史可比
        old = archived.get(holdings_store.entry_key(account, row["symbol"])) or archived.get(
            row["symbol"]
        )
        if not old or old.get("stance") == row["stance"]:
            continue
        changes.append(
            {
                "symbol": row["symbol"],
                "account": account,
                "from": old.get("stance"),
                "from_label": STANCE_LABELS.get(old.get("stance"), old.get("stance")),
                "to": row["stance"],
                "to_label": STANCE_LABELS.get(row["stance"], row["stance"]),
            }
        )
    return changes


def _position_state(panel: Any, symbol: str) -> dict[str, Any]:
    """One symbol's decision-relevant state, read live from the panel.

    缺字段就缺对应的数值（ATR 要 high/low，量比要 volume）——分析照常，
    只是少一条依据，而不是整段失败。
    """
    empty: dict[str, Any] = {
        "close": None,
        "change_pct": None,
        "rsi_14": None,
        "ma20": None,
        "ma60": None,
        "atr_14": None,
        "high20": None,
        "low20": None,
        "volume_ratio": None,
    }
    if symbol not in panel.prices.columns:
        return empty
    close = panel.prices[symbol].dropna()
    if len(close) < 2 or not close.iloc[-1]:
        return empty
    frame = close.to_frame(name=symbol)
    state = dict(empty)
    state["close"] = round(float(close.iloc[-1]), 3)
    prev = float(close.iloc[-2])
    state["change_pct"] = round((state["close"] / prev - 1) * 100, 2) if prev else None
    state["rsi_14"] = _clean(_last(rsi(frame)))
    state["ma20"] = _clean(_last(sma(frame, 20)))
    state["ma60"] = _clean(_last(sma(frame, 60)))
    window = close.tail(20)
    state["high20"] = round(float(window.max()), 3)
    state["low20"] = round(float(window.min()), 3)
    if panel.has_field("high") and panel.has_field("low"):
        highs = panel.field("high")[symbol]
        lows = panel.field("low")[symbol]
        state["atr_14"] = _clean(_last(atr(highs.to_frame(), lows.to_frame(), frame)))
    if panel.has_field("volume"):
        volume = panel.field("volume")[symbol].dropna()
        if len(volume) > 5:
            base = float(volume.iloc[-6:-1].mean())
            state["volume_ratio"] = None if not base else round(float(volume.iloc[-1]) / base, 2)
    return state


def _last(frame: pd.DataFrame) -> float:
    """Last value of an indicator frame, NaN when the column is still warming up."""
    if frame.empty:
        return float("nan")
    return float(frame.iloc[-1, 0])


def _portfolio_payload(settings: Settings) -> dict[str, Any]:
    """The book: stored cost/shares plus live quotes. No verdicts — that is
    /portfolio/review, which costs more to compute."""
    data = holdings_store.load(settings.data.directory)
    try:
        panel = _load_panel(settings.collection.provider, settings.collection.adjust)
    except HTTPException:
        panel = None
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})
    sector_map, _ = _sector_map(settings)
    rows = []
    for entry in data["positions"].values():
        symbol = str(entry.get("symbol"))
        account = str(entry.get("account") or holdings_store.DEFAULT_ACCOUNT)
        shares = float(entry.get("shares") or 0)
        cost = float(entry.get("cost") or 0)
        quote = _quote(panel, symbol)
        value = None if quote["last_close"] is None else round(shares * quote["last_close"], 2)
        pnl = None if value is None else round(value - shares * cost, 2)
        rows.append(
            {
                "symbol": symbol,
                "account": account,
                "name": name_of.get(symbol, ""),
                "sector": sector_map.get(symbol),
                "shares": shares,
                "cost": cost,
                "note": entry.get("note", ""),
                "added_at": entry.get("added_at"),
                **quote,
                "market_value": value,
                "pnl": pnl,
                "pnl_pct": None
                if pnl is None or not shares * cost
                else round(pnl / (shares * cost) * 100, 2),
            }
        )
    rows.sort(
        key=lambda row: (
            row["account"] != holdings_store.DEFAULT_ACCOUNT,
            row["account"],
            row["symbol"],
        )
    )

    # 每账户汇总：合计只算有现价的部分，缺现价的票单列——与逐票口径一致
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = summary.setdefault(
            row["account"],
            {
                "account": row["account"],
                "n_positions": 0,
                "market_value": 0.0,
                "cost": 0.0,
                "pnl": 0.0,
                "n_unpriced": 0,
            },
        )
        bucket["n_positions"] += 1
        bucket["cost"] += row["shares"] * row["cost"]
        if row["market_value"] is None:
            bucket["n_unpriced"] += 1
            continue
        bucket["market_value"] += row["market_value"]
        bucket["pnl"] += row["pnl"] or 0
    for bucket in summary.values():
        # 有票缺现价时百分比会失真（成本含全部、市值只含部分）：不给,
        # 前端按 n_unpriced 提示补采——不给错的比率强过给一个像样的错数
        if bucket["n_unpriced"] or not bucket["cost"]:
            bucket["pnl_pct"] = None
        else:
            bucket["pnl_pct"] = round(bucket["pnl"] / bucket["cost"] * 100, 2)
        for key in ("market_value", "cost", "pnl"):
            bucket[key] = round(bucket[key], 2)

    return {
        "positions": rows,
        "accounts": list(summary.values()),
        "account_names": holdings_store.accounts(data),
        "account_details": holdings_store.account_details(data),
    }


@router.get("/watchlist/analysis")
def watchlist_analysis() -> dict[str, Any]:
    """自选个股的技术截面 + 基于已落盘样本的大盘宽度。

    「大盘」在这里只能诚实地叫作「已落盘样本的等权统计」：项目没有接指数行情，
    拿 30 只票的等权均值冒充上证指数是编造。样本范围由 UI 明示——宽度数字的
    价值在于相对变化，不在于和官方指数对数。
    """
    settings = load_settings()
    data = watchlist_store.load(settings.data.directory)
    try:
        panel = _load_panel(settings.collection.provider, settings.collection.adjust)
        bundle = _load_indicators(settings.collection.provider, settings.collection.adjust)
    except HTTPException as exc:
        # 没有落盘数据时大盘卡也看得见——只是空着，并说明为什么空着
        return {
            "market": None,
            "market_curve": [],
            "stocks": [],
            "summary": None,
            "unavailable": str(exc.detail),
        }
    if data["stocks"] and not bundle.frames:
        pass  # bundle 为空不致命：只是截面列缺失

    close = panel.prices
    if len(close) < 2 or close.empty:
        raise HTTPException(status_code=400, detail="面板至少要两个交易日才能分析涨跌")
    last, prev = close.iloc[-1], close.iloc[-2]
    changes = last / prev - 1

    ma20 = bundle.frames.get("ma_20")
    above_ma20: pd.Series | None = None
    if ma20 is not None:
        aligned = ma20.iloc[-1].reindex(close.columns).dropna()
        above_ma20 = last.loc[aligned.index] > aligned

    window = close.tail(20)
    new_high = int((close.iloc[-1] >= window.max()).sum())
    new_low = int((close.iloc[-1] <= window.min()).sum())
    median_change = _clean(float(changes.median()) * 100)

    equal = close.mean(axis=1).dropna()
    curve_src = equal.tail(60)
    base = float(curve_src.iloc[0]) if float(curve_src.iloc[0]) else 1.0
    market_curve = [
        [str(idx.date()), round(float(v) / base * 100, 2)] for idx, v in curve_src.items()
    ]

    market = {
        "as_of": str(close.index[-1].date()),
        "n_symbols": len(changes),
        "up": int((changes > 0).sum()),
        "down": int((changes < 0).sum()),
        "flat": int((changes == 0).sum()),
        "median_change": median_change,
        "above_ma20_ratio": (
            _clean(float(above_ma20.mean()) * 100)
            if above_ma20 is not None and len(above_ma20)
            else None
        ),
        "new_high_20d": new_high,
        "new_low_20d": new_low,
    }

    rsi = bundle.frames.get("rsi_14")
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})

    stocks: list[dict[str, Any]] = []
    watch_changes: list[float] = []
    for symbol, entry in sorted(data["stocks"].items()):
        quote = _quote(panel, symbol)
        change = quote["change_pct"]
        if change is not None:
            watch_changes.append(change)

        rsi_value = rsi_state = None
        if rsi is not None and symbol in rsi.columns:
            series = rsi[symbol].dropna()
            if len(series):
                rsi_value = round(float(series.iloc[-1]), 1)
                rsi_state = (
                    "overbought" if rsi_value > 70 else "oversold" if rsi_value < 30 else "neutral"
                )

        position = None
        if above_ma20 is not None and symbol in above_ma20.index:
            position = "above" if bool(above_ma20[symbol]) else "below"

        stocks.append(
            {
                "symbol": symbol,
                "name": entry.get("name") or name_of.get(symbol, ""),
                "categories": entry.get("categories", []),
                "note": entry.get("note", ""),
                **quote,
                "rsi_14": rsi_value,
                "rsi_state": rsi_state,
                "ma20_position": position,
            }
        )

    watch_avg = sum(watch_changes) / len(watch_changes) if watch_changes else None
    summary = (
        {
            "watch_count": len(stocks),
            "watch_up": sum(1 for c in watch_changes if c > 0),
            "watch_down": sum(1 for c in watch_changes if c < 0),
            "avg_change": _clean(watch_avg),
            "vs_market": (
                _clean(watch_avg - median_change)
                if watch_avg is not None and median_change is not None
                else None
            ),
        }
        if data["stocks"]
        else None
    )
    return {"market": market, "market_curve": market_curve, "stocks": stocks, "summary": summary}


def _clean(value: float | None) -> float | None:
    """NaN 与 None 在 JSON 里是两回事：NaN 会变成非法的 ``NaN`` 字面量。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(value, 2)


# --- 板块 ---------------------------------------------------------------------


def _sector_map(settings: Settings) -> tuple[dict[str, str], str | None]:
    """``{symbol: sector}`` plus the table's refresh stamp, or ({} , None)."""
    frame = universe.load_sectors(settings.data.directory)
    if frame.empty:
        return {}, None
    stamp = str(frame["updated_at"].iloc[0]) if "updated_at" in frame.columns else None
    return dict(zip(frame["symbol"], frame["sector"])), stamp


@router.get("/sectors/movers")
def sector_movers(
    days: int = Query(default=5, ge=2, le=60), top: int = Query(default=8, ge=3, le=20)
) -> dict[str, Any]:
    """板块异动：近 ``days`` 日各板块成分股的等权涨幅与上涨占比。

    「异动」是相对的：一个板块涨幅排在全体板块分布的两端才值得看，绝对涨幅
    1% 在普涨日毫无信息。板块表未建立时 400 并给出建立命令——空表算出来的
    「无异动」和真无异动是两回事。
    """
    settings = load_settings()
    sector_map, stamp = _sector_map(settings)
    if not sector_map:
        raise HTTPException(
            status_code=400,
            detail="板块表尚未建立：先运行 quant-symbols --sectors 建立行业归属",
        )

    panel = _load_panel(settings.collection.provider, settings.collection.adjust)
    close = panel.prices
    if len(close) < days + 1:
        raise HTTPException(
            status_code=400, detail=f"面板不足 {days + 1} 个交易日，算不了 {days} 日板块涨幅"
        )

    window_change = (close.iloc[-1] / close.iloc[-1 - days] - 1) * 100

    rows: list[dict[str, Any]] = []
    grouped = pd.DataFrame({"sector": pd.Series(sector_map), "change": window_change}).dropna()
    for sector, chunk in grouped.groupby("sector"):
        rows.append(
            {
                "sector": str(sector),
                "n": len(chunk),
                "change": _clean(float(chunk["change"].mean())),
                "up_ratio": _clean(float((chunk["change"] > 0).mean()) * 100),
            }
        )
    rows.sort(key=lambda item: item["change"] if item["change"] is not None else -1e9, reverse=True)
    if len(rows) <= top:
        leading, lagging = rows, []
    else:
        leading, lagging = rows[:top], list(reversed(rows[-top:]))

    return {
        "days": days,
        "as_of": str(close.index[-1].date()),
        "updated_at": stamp,
        "n_sectors": len(rows),
        "leading": leading,
        "lagging": lagging,
        "all": rows,
    }


@router.get("/sectors/analysis")
def sector_analysis(
    days: int = Query(default=5, ge=2, le=60),
    provider: str | None = None,
    adjust: str | None = None,
) -> dict[str, Any]:
    """板块分析：每个板块的汇总 + 板块内的领涨/领跌个股。

    与 ``/sectors/movers`` 的区别：movers 只回答「哪些板块在动」，这里回答
    「动的是谁」——多了分化度（板块内标准差：高则选股比选板块重要）、
    中位涨幅（不被一两只权重带偏）、成交额与板块内个股。
    """
    settings = load_settings()
    sector_map, stamp = _sector_map(settings)
    if not sector_map:
        raise HTTPException(
            status_code=400,
            detail="板块表尚未建立：先运行 quant-symbols --sectors 建立行业归属",
        )
    source = provider or settings.collection.provider
    basis = adjust or settings.collection.adjust
    panel = _load_panel(source, basis)
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})
    snapshot = overseas_mod.load(settings.data.directory)
    try:
        rows = sectors_mod.analyze(panel, sector_map, name_of, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "days": days,
        "provider": source,
        "as_of": str(panel.prices.index[-1].date()),
        "updated_at": stamp,
        "n_sectors": len(rows),
        "n_symbols": int(panel.prices.shape[1]),
        # 有板块归属且在面板里的票：覆盖率和「板块涨幅」的可信度直接相关
        "n_covered": sum(row.n for row in rows),
        "sectors": [row.to_dict() for row in rows],
        # 外围关联：只读本地快照，不联网——板块页刷新不该变成一次抓取。
        # 关键词匹配不到、或锚品种行情缺失的板块不出现，宁缺毋假。
        "links": {
            row.sector: link
            for row in rows
            if (link := overseas_mod.sector_link(row.sector, snapshot)) is not None
        },
        "overseas_fetched_at": snapshot.get("fetched_at") if snapshot else None,
    }


@router.get("/sectors/members")
def sector_members(
    sector: str,
    days: int = Query(default=5, ge=2, le=60),
    provider: str | None = None,
    adjust: str | None = None,
) -> dict[str, Any]:
    """单个板块的成分股明细：涨跌、相对板块超额、RSI、MA20、量比、成交额。

    「超额」是相对本板块等权涨幅——板块普涨时，跑输板块的票本身就是信号。
    """
    settings = load_settings()
    sector_map, stamp = _sector_map(settings)
    if not sector_map:
        raise HTTPException(
            status_code=400,
            detail="板块表尚未建立：先运行 quant-symbols --sectors 建立行业归属",
        )
    source = provider or settings.collection.provider
    basis = adjust or settings.collection.adjust
    panel = _load_panel(source, basis)
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})
    try:
        rows = sectors_mod.members(panel, sector_map, sector, name_of, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"板块「{sector}」没有可分析的成分股：名字对不上，或这些票还没有行情",
        )
    return {
        "sector": sector,
        "days": days,
        "provider": source,
        "as_of": str(panel.prices.index[-1].date()),
        "updated_at": stamp,
        "n": len(rows),
        "members": [member.to_dict() for member in rows],
    }


@router.get("/volume/analysis")
def volume_analysis(provider: str | None = None, adjust: str | None = None) -> dict[str, Any]:
    """量能分析：大盘 / 板块 / 个股三层，每层给量价状态与一句可执行的话。

    三层都和自己（或自己所属层级）的 20 日均量比，不和绝对成交额比——普涨日
    的成交额天然放大，绝对数字没有信息。

    个股优先取「持仓 ∪ 自选」——量能提示只有落在自己关心的票上才有用；
    两者都空时退回成交额前 8，并在 ``stock_source`` 里说明这是替代方案。
    """
    settings = load_settings()
    source = provider or settings.collection.provider
    basis = adjust or settings.collection.adjust
    try:
        panel = _load_panel(source, basis)
    except HTTPException as exc:
        return {
            "as_of": None,
            "provider": source,
            "market": None,
            "sectors": [],
            "stocks": [],
            "unavailable": str(exc.detail),
        }
    rules = VolumeRules()
    sector_map, stamp = _sector_map(settings)
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})
    market_dict, market_source = _market_volume(settings)
    if market_dict is None:
        return {
            "as_of": None,
            "provider": source,
            "market": None,
            "sectors": [],
            "stocks": [],
            "unavailable": "面板与指数缓存都不可用：先补数据",
        }

    watch = watchlist_store.load(settings.data.directory).get("stocks", {})
    book = holdings_store.symbols(holdings_store.load(settings.data.directory))
    symbols = sorted(set(watch) | set(book))
    origin = "持仓 + 自选"
    if not symbols:
        if panel.has_field("amount"):
            ranking = panel.field("amount").iloc[-1].sort_values(ascending=False)
        else:
            ranking = panel.field("volume").iloc[-1].sort_values(ascending=False)
        symbols = [symbol for symbol in ranking.index[:8]]
        origin = "成交额前 8（还没有持仓与自选）"

    return {
        "as_of": market_dict["as_of"],
        "provider": source,
        "market_source": market_source,
        "updated_at": stamp,
        "sector_table": bool(sector_map),
        "market": market_dict,
        "sectors": [row.to_dict() for row in volume_mod.sector(panel, sector_map, name_of, rules)]
        if sector_map
        else [],
        "stocks": [
            row.to_dict() for row in volume_mod.stock(panel, symbols, sector_map, name_of, rules)
        ],
        "stock_source": origin,
    }


@router.get("/overseas/analysis")
def overseas_analysis() -> dict[str, Any]:
    """外围盘分析：只读本地快照，不联网、不写盘。

    快照由 ``POST /overseas/refresh`` 抓取。GET 里做网络请求会让每次刷新
    页面都变成一次不可控的抓取，也会让「数据是几点抓的」失去意义。
    """
    snapshot = overseas_mod.load(load_settings().data.directory)
    if snapshot is None:
        return {
            "available": False,
            "reason": "还没有外围快照——点「刷新」抓取一次（需要外网）",
        }
    payload = overseas_mod.analyze(snapshot)
    payload["available"] = True
    return payload


@router.post("/overseas/refresh")
def overseas_refresh() -> dict[str, Any]:
    """抓一次外围快照并落盘。部分接口失败也保存——有几项分析几项。"""
    settings = load_settings()
    try:
        snapshot = overseas_mod.fetch()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"外围数据抓取失败：{type(exc).__name__} {exc}"
        ) from exc
    if not snapshot["quotes"] and not snapshot["rates"]:
        raise HTTPException(
            status_code=503,
            detail="外围数据一项都没抓到（外网不可达？）："
            + "；".join(snapshot.get("missing") or []),
        )
    overseas_mod.save(settings.data.directory, snapshot)
    payload = overseas_mod.analyze(snapshot)
    payload["available"] = True
    payload["saved"] = True
    return payload


@router.get("/playbook")
def playbook() -> dict[str, Any]:
    """战法清单 + 今日环境标注。

    环境取两个现成的分析输出：昨日量价四象限（volume.market）与外围情绪分
    （overseas）。缺一块就少一块标注——「今日：数据不足」和「今日无事可做」
    都会如实标注，战法本体永远照给：缺环境不等于战法失效。
    """
    settings = load_settings()
    volume_env, _source = _market_volume(settings)
    snapshot = overseas_mod.load(settings.data.directory)
    tone_env = overseas_mod.analyze(snapshot)["tone"] if snapshot else None
    return playbook_mod.analyze(volume_env, tone_env)


# 可编码进日线回测的战法：key → (策略注册名, 默认参数)。
# 金字塔加仓、ATR 定仓、盘中触发这类细节不在日线等权回测的编码范围内，
# 结果只回答「这条规则在本样本上的毛收益轮廓」，不是战法全文的验证。
TACTIC_STRATEGIES: dict[str, tuple[str, dict[str, float]]] = {
    "turtle": ("turtle", {}),
    "breakout-20d-high": ("breakout-20d-high", {}),
    "pullback-ma20": ("pullback-ma20", {}),
}


def _indicator_registry(settings) -> indicators_mod.IndicatorRegistry:
    """用户自定义指标的持久注册表（data/custom_indicators.json）。"""
    return indicators_mod.IndicatorRegistry(
        Path(settings.data.directory) / "custom_indicators.json"
    )


@router.get("/strategies/custom/indicators")
def custom_indicators_list() -> dict[str, Any]:
    """自定义指标清单 + 内置指标口径：界面语法说明区直接渲染。"""
    registry = _indicator_registry(load_settings())
    return {
        "custom": registry.list(),
        "builtin": custom_mod.METRIC_SPEC,
        "grammar": indicators_mod.ARITHMETIC_GRAMMAR_NOTE,
    }


@router.post("/strategies/custom/indicators")
def custom_indicators_define(payload: dict[str, Any]) -> dict[str, Any]:
    """新增或重定义自定义指标：先语法检测（只许引用内置与更早定义），
    除零/窗口不足算出「不知道」不触发——口径写进说明展示。"""
    registry = _indicator_registry(load_settings())
    result = registry.define(
        str(payload.get("name") or ""),
        str(payload.get("expr") or ""),
        str(payload.get("desc") or ""),
    )
    if not result["ok"]:
        raise HTTPException(
            status_code=400, detail="；".join(e["message"] for e in result["errors"])
        )
    return result


@router.delete("/strategies/custom/indicators/{name}")
def custom_indicators_delete(name: str) -> dict[str, Any]:
    registry = _indicator_registry(load_settings())
    if not registry.remove(name):
        raise HTTPException(status_code=404, detail=f"没有叫「{name}」的自定义指标")
    return {"removed": name, "indicators": registry.list()}


@router.get("/strategies/custom/spec")
def custom_strategy_spec() -> dict[str, Any]:
    """自定义策略 DSL 的语法说明：界面直接渲染给用户看。"""
    registry = _indicator_registry(load_settings())
    return {
        "metrics": {
            **custom_mod.METRIC_SPEC,
            **{item["name"].lower(): item["desc"] for item in registry.list()},
        },
        "custom_indicators": registry.list(),
        "arithmetic_note": indicators_mod.ARITHMETIC_GRAMMAR_NOTE,
        "operators": {
            "compare": [">", ">=", "<", "<=", "=="],
            "logic": ["AND", "OR（不区分大小写）"],
            "note": "比较右边可以是数字或另一个指标（如 close > ma20）；AND 优先于 OR，括号改变优先级",
        },
        "examples": [
            "close > ma20 AND vol_ratio > 1.5",
            "close > high20 OR (chg > 5 AND vol_ratio > 2)",
            "close < low20 AND chg < -2",
        ],
        "notes": [
            "出场条件留空 = 持有到样本结束（如实标注，不假装有出场）",
            "窗口不足时指标为「不知道」，不触发信号（如上市不满 20 日的前高）",
            "表达式在「0/1 信号 × 等权持有 × 含成本」的日线回测里跑——金字塔、盘中触发不在表达范围",
        ],
    }


@router.post("/strategies/custom/validate")
def custom_strategy_validate(payload: dict[str, Any]) -> dict[str, Any]:
    """语法初步检测：逐条带字符位置。语法不挡语义——「close > ma20 AND
    vol_ratio > 2」语法对但可能从未触发，那是回测要回答的问题。"""
    available = _indicator_registry(load_settings()).available()
    errors = custom_mod.validate_expr(str(payload.get("expr") or ""), available)
    exit_errors = (
        custom_mod.validate_expr(str(payload.get("exit_expr") or ""), available)
        if payload.get("exit_expr")
        else []
    )
    return {
        "ok": not errors and not exit_errors,
        "errors": [{"pos": err.pos, "message": err.message, "field": "expr"} for err in errors]
        + [{"pos": err.pos, "message": err.message, "field": "exit_expr"} for err in exit_errors],
    }


@router.post("/strategies/custom/backtest")
def custom_strategy_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    """跑一条自定义策略的本样本回测：口径与战法验证一致。

    先语法检测（400 + 逐条错误），再算信号——语法错了别浪费一次整
    面板回测。从未触发如实说，不给空转净值。
    """
    expr = str(payload.get("expr") or "")
    exit_expr = str(payload.get("exit_expr") or "") or None
    registry = _indicator_registry(load_settings())
    available = registry.available()
    errors = custom_mod.validate_expr(expr, available) + (
        custom_mod.validate_expr(exit_expr, available) if exit_expr else []
    )
    if errors:
        raise HTTPException(
            status_code=400,
            detail="；".join(err.message for err in errors[:5]),
        )

    settings = load_settings()
    try:
        panel = _load_panel(settings.collection.provider, settings.collection.adjust)
    except HTTPException as exc:
        raise HTTPException(status_code=400, detail=f"面板不可用：{exc.detail}") from None

    try:
        kwargs: dict[str, Any] = {
            "expr": expr,
            "exit_expr": exit_expr,
            "indicators": registry.list(),
        }
        if panel.has_field("amount"):
            kwargs["amount"] = panel.field("amount")
        signal = get_signal("custom")(panel.prices, **kwargs)
    except ValueError as exc:
        # vol_ratio 需要成交额而面板缺失——如实拒绝，不静默放行量条件
        raise HTTPException(status_code=400, detail=str(exc)) from None

    if not bool(signal.to_numpy().any()):
        return {
            "available": False,
            "as_of": None,
            "reason": "全样本从未触发——条件太严或面板窗口不足，不编结果",
            "n_symbols": len(panel.symbols),
        }

    config = BacktestConfig.from_settings(settings)
    result = run_signal_backtest(panel.prices, signal, weight_method="equal_weight", config=config)
    metrics = _metrics(result.summary(benchmark_buy_hold(panel.prices)))
    return {
        "available": True,
        "as_of": str(panel.prices.index[-1].date()),
        "metrics": metrics,
        "n_symbols": len(panel.symbols),
        "n_days_in_market": int(signal.to_numpy().sum()),
        "n_symbols_with_signal": int((signal.to_numpy().sum(axis=0) > 0).sum()),
        "note": "出场条件留空 = 持有到样本结束" if not exit_expr else None,
    }


@router.get("/volume/ranking")
def volume_ranking() -> dict[str, Any]:
    """全样本量比排行（只读缓存，不联网）。

    量比口径与 volume._window_stats 一致：当日额 / 前 20 日均额
    （shift(1)，今天的放量不抬高自己的基线）。这是 238 只股票样本的
    排行——ETF 不在其中（它们在 ETF 视图有自己的量比）；缺成交额的
    样本退回成交量并如实标注口径。
    """
    settings = load_settings()
    try:
        panel = _load_panel(settings.collection.provider, settings.collection.adjust)
    except HTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    if panel.has_field("amount"):
        field = panel.field("amount")
        metric = "amount"
    elif panel.has_field("volume"):
        field = panel.field("volume")
        metric = "volume"
    else:
        return {"available": False, "reason": "面板没有成交额/成交量列：先补数据"}

    if len(field) < 21:
        return {
            "available": False,
            "reason": f"面板只有 {len(field)} 天：量比需要至少 21 天（前 20 日均量做基线）",
        }

    baseline = field.iloc[-21:-1].mean()
    today = field.iloc[-1]
    close = panel.prices  # prices 即 close 面板（date × symbol）
    prev_close = close.iloc[-2] if len(close) >= 2 else None
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}

    rows: list[dict[str, Any]] = []
    for symbol in field.columns:
        base = baseline[symbol]
        if not base or pd.isna(base):
            continue
        ratio = float(today[symbol] / base)
        day_change = (
            float(close[symbol].iloc[-1] / prev_close[symbol] - 1)
            if prev_close is not None and prev_close[symbol] and not pd.isna(prev_close[symbol])
            else None
        )
        rows.append(
            {
                "symbol": symbol,
                "name": name_of.get(symbol, symbol),
                "vol_ratio": ratio,
                "day_change": day_change,
            }
        )
    rows.sort(key=lambda row: row["vol_ratio"], reverse=True)
    return {
        "available": bool(rows),
        "metric": metric,
        "as_of": str(field.index[-1].date()),
        "n_symbols": len(rows),
        "top_in": rows[:15],
        "top_out": list(reversed(rows[-10:])),
        "note": f"量比口径：{'成交额' if metric == 'amount' else '成交量（缺成交额，如实退回）'} 当日 / 前 20 日均（不含当日）；范围 = 样本面板全部股票，ETF 不在其中",
    }


@router.get("/indexes")
def indexes_compare() -> dict[str, Any]:
    """国内主要指数对比：只读缓存。没采过的进 missing，不静默少行。"""
    return index_mod.compare(load_settings().data.directory)


@router.get("/etf/snapshot")
def etf_snapshot(adjust: str = "qfq") -> dict[str, Any]:
    """ETF 清单快照：只读缓存。没采集过的代码进 missing，绝不悄悄消失。"""
    return _etf_snapshot_view(adjust)


def _parse_watchlist_lines(text: str) -> list[dict[str, Any]]:
    """每行 `代码 [名称] [#分类...]`：600519 贵州茅台 #白酒 #消费。

    只认空格分隔的第一段为代码，其余自由文本里抓 # 开头的分类；
    名称是第二段去掉 # 后的剩余部分（可空）。
    """
    entries: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        tokens = line.split()
        if not tokens:
            continue
        categories = [token[1:] for token in tokens if token.startswith("#") and len(token) > 1]
        rest = [token for token in tokens if not token.startswith("#")]
        name = " ".join(rest[1:]) if len(rest) > 1 else ""
        entries.append({"symbol": rest[0], "name": name, "categories": categories})
    return entries


def _parse_position_lines(text: str) -> list[dict[str, Any]]:
    """每行 `代码 股数 成本`：600519 1000 1500.5。"""
    entries: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if len(tokens) < 3:
            entries.append({"symbol": tokens[0], "shares": None, "cost": None})
            continue
        entries.append({"symbol": tokens[0], "shares": tokens[1], "cost": tokens[2]})
    return entries


_EXCEL_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    # 宽容映射：用户表格的表头五花八门，能对上的都对上
    "symbol": ("代码", "证券代码", "股票代码", "代码/code", "symbol", "code"),
    "name": ("名称", "股票名称", "证券简称", "名称/name", "name"),
    "categories": ("分类", "分组", "标签", "板块", "备注分类"),
    "shares": ("股数", "数量", "持仓数量", "持股数", "shares", "数量/股"),
    "cost": ("成本", "成本价", "买入价", "成本单价", "持仓成本", "cost"),
    "account": ("账户", "账号", "券商账户", "account"),
}


@router.get("/import/excel/spec")
def excel_import_spec() -> dict[str, Any]:
    """Excel 导入的格式说明：界面直接渲染，别名与解析代码同源。

    说明写死在文档里就会和 _EXCEL_HEADER_ALIASES 漂移——改了别名
    忘了改说明，用户按说明写的表头反而导不进来。这里从同一份常量
    出，说明永远等于实现。
    """
    return {
        "required": {"portfolio": ["symbol", "shares", "cost"], "watchlist": ["symbol"]},
        "optional": {
            "portfolio": ["name", "account"],
            "watchlist": ["name", "categories"],
        },
        "aliases": {field: list(aliases) for field, aliases in _EXCEL_HEADER_ALIASES.items()},
        "rules": [
            "只读第一张工作表（文件打开时激活的那张）",
            "表头必须在第 1 行——上面不要留标题行",
            "整行空白的空行自动跳过；报错里的行号是 Excel 里的真实行号",
            "代码写法宽容：600519、600519.SH 都认",
            "持仓：股数与成本必须是正数；已在持仓里的票默认跳过，不覆盖真实成本",
            "自选：已在自选里的票跳过，不覆盖已有分类与备注",
            "分类列用逗号或顿号分隔多个（如「白酒, 核心」）",
            "持仓可加「账户」列分开记不同券商；留空进默认账户，同一票不同账户是两条独立持仓",
            "支持 .xlsx；老 .xls 请先另存为 .xlsx。公式单元格读缓存值——在 Excel 里打开保存过一次即可",
        ],
        "examples": {
            "portfolio": [
                ["代码", "股数", "成本价", "账户"],
                ["600519", 100, 1500.5, "默认"],
                ["000333", 20000, 55.2, ""],
            ],
            "watchlist": [
                ["代码", "名称", "分类"],
                ["600519", "贵州茅台", "白酒, 核心"],
                ["300750", "宁德时代", ""],
            ],
        },
    }


def _rows_from_excel(content: bytes) -> tuple[list[str], list[dict[str, object]]]:
    """读工作簿第一个 sheet：返回 (表头, 行字典列表)。

    表头必须能认出「代码」列——认不出来就如实报错并列出实际表头，
    不猜。空行（整行无值）跳过；数字单元格可能是 int/float/str，原样
    交给下游的解析与校验。
    """
    import io

    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"读不了这个文件：{type(exc).__name__}: {exc}"
        ) from exc
    sheet = workbook.active
    if sheet is None:
        raise HTTPException(status_code=400, detail="工作簿没有工作表")
    rows_iter = sheet.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if header is None:
        raise HTTPException(status_code=400, detail="表格是空的")
    headers = [str(cell).strip() if cell is not None else "" for cell in header]
    rows: list[dict[str, object]] = []
    line = 1  # 表头占第 1 行；空行也占号——报告要用 Excel 行号，错位比拒绝更误导
    for values in rows_iter:
        line += 1
        if values is None or all(value is None or str(value).strip() == "" for value in values):
            continue
        row = {get_column_letter(i + 1): value for i, value in enumerate(values)}
        row["_line"] = line
        rows.append(row)
    return headers, rows


def _map_excel_headers(headers: list[str]) -> dict[str, str]:
    """表头 → 标准字段名；返回 {标准名: 列字母}。找不到代码列直接报错。"""
    lowered = {
        h.strip().lower(): get_column_letter(i + 1) for i, h in enumerate(headers) if h.strip()
    }
    mapping: dict[str, str] = {}
    for field, aliases in _EXCEL_HEADER_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lowered:
                mapping[field] = lowered[alias.lower()]
                break
    if "symbol" not in mapping:
        raise HTTPException(
            status_code=400,
            detail=f"没认出「代码」列。实际表头：{headers}。把代码列命名为「代码」或「证券代码」",
        )
    return mapping


def _excel_field(row: dict[str, object], mapping: dict[str, str], field: str) -> object:
    """取一个（可能是可选的）字段：表里没有这一列就是 None，不是 KeyError。"""
    column = mapping.get(field)
    if column is None:
        return None
    value = row.get(column)
    return value if value is not None else None


@router.post("/watchlist/import/excel")
async def watchlist_import_excel(file: UploadFile) -> dict[str, Any]:
    """Excel 批量导入自选：列「代码 [名称] [分类]」，表头宽容映射。

    与粘贴导入同一套诚实约定：已在自选里的不动、invalid 逐条带行号。
    """
    content = await file.read()
    headers, rows = _rows_from_excel(content)
    mapping = _map_excel_headers(headers)
    entries: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, str]] = []
    lines: list[int] = []  # entries[i] 来自 Excel 第几行（1 是表头）
    for row in rows:
        symbol = _excel_field(row, mapping, "symbol")
        if symbol is None or str(symbol).strip() == "":
            skipped_rows.append({"line": str(row["_line"]), "reason": "代码为空"})
            continue
        lines.append(int(str(row.pop("_line"))))  # object → str → int
        raw_categories = _excel_field(row, mapping, "categories")
        categories = (
            [
                c.strip()
                for c in str(raw_categories).replace("，", ",").replace("、", ",").split(",")
                if c.strip()
            ]
            if raw_categories is not None
            else []
        )
        entries.append(
            {
                "symbol": str(symbol).strip(),
                "name": str(_excel_field(row, mapping, "name") or "").strip(),
                "categories": categories,
            }
        )
    result = watchlist_store.import_stocks(load_settings().data.directory, entries)
    invalid = [
        {**item, "line": str(lines[int(item["line"]) - 1])}  # 行号换成 Excel 行号
        for item in result["invalid"]
    ]
    return {**result, "n_added": len(result["added"]), "invalid": [*invalid, *skipped_rows]}


@router.post("/portfolio/import/excel")
async def portfolio_import_excel(file: UploadFile) -> dict[str, Any]:
    """Excel 批量导入持仓：列「代码 股数 成本」，默认跳过已有持仓。"""
    content = await file.read()
    headers, rows = _rows_from_excel(content)
    mapping = _map_excel_headers(headers)
    missing = [field for field in ("shares", "cost") if field not in mapping]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"没认出{'和'.join('「股数」' if m == 'shares' else '「成本价」' for m in missing)}列。实际表头：{headers}",
        )
    entries: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, str]] = []
    lines: list[int] = []
    for row in rows:
        symbol = _excel_field(row, mapping, "symbol")
        if symbol is None or str(symbol).strip() == "":
            skipped_rows.append({"line": str(row["_line"]), "reason": "代码为空"})
            continue
        lines.append(int(str(row.pop("_line"))))  # object → str → int
        entries.append(
            {
                "symbol": str(symbol).strip(),
                "shares": _excel_field(row, mapping, "shares"),
                "cost": _excel_field(row, mapping, "cost"),
                "account": str(_excel_field(row, mapping, "account") or "").strip() or None,
            }
        )
    result = holdings_store.import_positions(load_settings().data.directory, entries)
    invalid = [{**item, "line": str(lines[int(item["line"]) - 1])} for item in result["invalid"]]
    return {**result, "n_added": len(result["added"]), "invalid": [*invalid, *skipped_rows]}


@router.post("/watchlist/import")
def watchlist_import(payload: dict[str, Any]) -> dict[str, Any]:
    """批量加入自选：幂等（已在表里的不动）、逐条报告成败。"""
    text = str(payload.get("text") or "")
    result = watchlist_store.import_stocks(
        load_settings().data.directory, _parse_watchlist_lines(text)
    )
    return {**result, "n_added": len(result["added"])}


@router.post("/portfolio/import")
def portfolio_import(payload: dict[str, Any]) -> dict[str, Any]:
    """批量录入持仓：默认跳过已有持仓，不覆盖真实成本。"""
    text = str(payload.get("text") or "")
    result = holdings_store.import_positions(
        load_settings().data.directory, _parse_position_lines(text)
    )
    return {**result, "n_added": len(result["added"])}


@router.get("/etf/compare")
def etf_compare(symbols: str, days: int = Query(250, ge=20, le=1500)) -> dict[str, Any]:
    """多只 ETF 归一化对比（首日=100）：曲线 + 统计 + 相关性。

    超过 6 只不给画：一张图上七条线，读者只会看见一团面条——宁可
    让用户分两次看，也不给看不懂的图。
    """
    wanted = [part.strip() for part in symbols.split(",") if part.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail="至少选一只 ETF")
    if len(wanted) > 6:
        raise HTTPException(status_code=400, detail="一次最多对比 6 只：再多就看不清了，分两次看")
    try:
        return etf_mod.compare(load_settings().data.directory, wanted, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.get("/etf/{symbol}/history")
def etf_history(symbol: str, days: int = Query(250, ge=20, le=1500)) -> dict[str, Any]:
    """一只 ETF 的历史走势与区间统计（只读缓存，不联网）。

    缺数据 404 并指路：ETF 是独立落盘，不在股票面板里——「没采」不是
    「没有」，用户点一次刷新就有。
    """
    try:
        return etf_mod.history(load_settings().data.directory, symbol, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.post("/etf/{symbol}/refresh")
def etf_refresh_one(symbol: str, adjust: str = "qfq") -> dict[str, Any]:
    """单只 ETF 联网采集：详情页「更新这只」用。

    整批刷新一轮十几只，用户等的是半分钟；看某一只时只需要这一只的新
    数据。失败如实 400 带上原因，不返回一个「成功」的空壳。
    """
    settings = load_settings()
    bare = etf_mod.to_bare_symbol(symbol)
    try:
        frame = etf_mod.refresh(settings.data.directory, bare, adjust=adjust)
    except Exception as exc:  # noqa: BLE001 - 采集异常原样上报
        raise HTTPException(
            status_code=400, detail=f"{bare} 采集失败：{type(exc).__name__}: {exc}"
        ) from None
    return {
        "symbol": bare,
        "name": etf_mod.ETF_UNIVERSE.get(bare, ""),
        "rows": len(frame),
        "as_of": str(frame.index[-1].date()) if len(frame) else None,
        "snapshot": etf_mod.snapshot(settings.data.directory, adjust=adjust),
    }


@router.post("/etf/refresh")
def etf_refresh(adjust: str = "qfq") -> dict[str, Any]:
    """逐只联网采集 ETF 日线（东财 fund_etf_hist_em）。"""
    return _etf_refresh_view(adjust)


@router.post("/playbook/verify")
def playbook_verify() -> dict[str, Any]:
    """对可编码战法跑本样本回测，把「胜率未知」变成「本样本上的轮廓」。

    POST 而非 GET：整面板 × 3 条策略的向量化计算是重活，不该在页面
    刷新的路径上。样本范围（多少只、多少天）随响应返回——脱离样本
    范围谈回测数字都是编造。整样本从未触发的战法如实说「未触发」，
    不给一个空转的净值曲线。
    """
    settings = load_settings()
    try:
        panel = _load_panel(settings.collection.provider, settings.collection.adjust)
    except HTTPException as exc:
        raise HTTPException(status_code=400, detail=f"面板不可用：{exc.detail}") from None

    results: list[dict[str, Any]] = []
    for key, (strategy, params) in TACTIC_STRATEGIES.items():
        factory = get_signal(strategy)
        kwargs: dict[str, Any] = dict(params)
        if "amount" in inspect.signature(factory).parameters and panel.has_field("amount"):
            kwargs["amount"] = panel.field("amount")
        signal = factory(panel.prices, **kwargs)
        if not bool(signal.to_numpy().any()):
            results.append(
                {
                    "key": key,
                    "name": next(p.name for p in playbook_mod.PLAYS if p.key == key),
                    "available": False,
                    "reason": "全样本从未触发——规则太严或面板窗口不足，不编结果",
                }
            )
            continue
        config = BacktestConfig.from_settings(settings)
        result = run_signal_backtest(
            panel.prices, signal, weight_method="equal_weight", config=config
        )
        metrics = _metrics(result.summary(benchmark_buy_hold(panel.prices)))
        results.append(
            {
                "key": key,
                "name": next(p.name for p in playbook_mod.PLAYS if p.key == key),
                "available": True,
                "metrics": metrics,
                "n_symbols_with_signal": int((signal.sum() > 0).sum()),
                "n_days_in_market": int(signal.to_numpy().sum()),
            }
        )

    return {
        "available": True,
        "as_of": str(panel.prices.index[-1].date()),
        "n_symbols": int(panel.prices.shape[1]),
        "n_days": len(panel.prices),
        "results": results,
    }


def _market_volume(settings: Any) -> tuple[dict[str, Any] | None, str]:
    """大盘量能：优先真实指数（只读缓存不联网），缺缓存退回样本合计。

    两条路都走 volume._market_from_series 同一套分析——量比、四象限、
    streak 的语义完全一致，变的只是成交额的来源。返回 (市场行, 来源)，
    来源是 ``index:<symbol>`` 或 ``sample``，前端如实显示。
    """
    symbol = next(iter(index_mod.INDEXES))
    frame = index_mod.load(settings.data.directory, symbol)
    if frame is not None:
        try:
            return volume_mod.market_from_index(frame).to_dict(), f"index:{symbol}"
        except ValueError:
            pass  # 指数缓存太短，退回样本路径
    try:
        panel = _load_panel(settings.collection.provider, settings.collection.adjust)
        return volume_mod.market(panel).to_dict(), "sample"
    except (HTTPException, ValueError):
        return None, "sample"


@router.get("/sector-flow")
def sector_flow() -> dict[str, Any]:
    """行业资金流快照（东财口径）：只读缓存，不联网、不写盘。

    东财行业分类与面板的证监会分类不是一套——快照不与板块表做跨口径
    匹配，宁可在页面上独立成卡注明口径，也不产出看似精确的错配。
    """
    root = load_settings().data.directory
    snapshot = sector_flow_mod.load(root)
    if snapshot is None:
        return {
            "available": False,
            "reason": "还没有行业资金流快照：点「刷新（联网抓取）」，或等 nightly 自动更新",
        }
    return {
        **snapshot,
        "available": True,
        "delta": sector_flow_mod.delta(root),  # 风向对比：只有一份快照时如实说不可比
    }


@router.post("/sector-flow/refresh")
def sector_flow_refresh() -> dict[str, Any]:
    """联网抓取行业资金流并落盘，返回同一份快照。"""
    snapshot = sector_flow_mod.fetch()
    sector_flow_mod.save(load_settings().data.directory, snapshot)
    return {**snapshot, "available": True, "saved": True}


def _etf_watch_count(settings: Any) -> int:
    """自选 ∪ 持仓里的 ETF 只数——红旗卡要如实标注「豁免 N 只」。"""
    watch = watchlist_store.load(settings.data.directory).get("stocks", {})
    book = holdings_store.symbols(holdings_store.load(settings.data.directory))
    symbols = set(watch) | set(book)
    return sum(1 for symbol in symbols if etf_mod.is_etf(symbol))


def _etf_snapshot_view(adjust: str) -> dict[str, Any]:
    """ETF 快照（GET：只读缓存）。ETF 落盘在 processed/etf，独立于
    股票面板——「样本合计」的大盘口径不被场内基金污染。"""
    return etf_mod.snapshot(load_settings().data.directory, adjust=adjust)


def _etf_refresh_view(adjust: str) -> dict[str, Any]:
    """逐只联网采集 ETF 日线；一只失败不拖垮整批，结果逐条可见。"""
    root = load_settings().data.directory
    fetched: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for bare in sorted(etf_mod.ETF_UNIVERSE):
        try:
            frame = etf_mod.refresh(root, bare, adjust=adjust)
            fetched.append(
                {
                    "symbol": bare,
                    "name": etf_mod.ETF_UNIVERSE[bare],
                    "rows": len(frame),
                    "as_of": str(frame.index[-1].date()),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单只失败逐条上报
            failed.append({"symbol": bare, "error": str(exc)})
    spot: dict[str, Any] | None = None
    try:
        spot = etf_mod.refresh_spot(root)
    except Exception as exc:  # noqa: BLE001 - 份额快照失败不拖垮日线结果
        failed.append({"symbol": "spot", "error": f"份额快照失败：{exc}"})
    snapshot = etf_mod.snapshot(root, adjust=adjust)
    return {
        **snapshot,
        "fetched": fetched,
        "failed": failed,
        "spot": spot,
        "saved": bool(fetched),
        "available": bool(fetched) or snapshot["available"],
    }


def _watch_symbols(settings: Any) -> list[str]:
    """持仓 ∪ 自选中的股票：基本面核对只落在自己关心的票上。

    ETF 被刻意排除——场内基金没有季报，「没查」和「豁免」必须分清：
    送 ETF 去查财报只会失败，还把红旗卡的「未覆盖」变成噪音。前端
    红旗卡会标注被豁免的 ETF 只数。
    """
    watch = watchlist_store.load(settings.data.directory).get("stocks", {})
    book = holdings_store.symbols(holdings_store.load(settings.data.directory))
    symbols = sorted(set(watch) | set(book))
    return [symbol for symbol in symbols if not etf_mod.is_etf(symbol)]


def _risk_to_dict(risk: Any, name_of: dict[str, str]) -> dict[str, Any]:
    return {
        "symbol": risk.symbol,
        "name": name_of.get(risk.symbol, risk.symbol),
        "level": risk.level,
        "score": risk.score,
        "likely_financial": risk.likely_financial,
        "period": risk.snapshot.period if risk.snapshot else None,
        "published": risk.snapshot.published if risk.snapshot else None,
        "flags": [
            {
                "code": flag.code,
                "label": flag.label,
                "value": flag.value,
                "threshold": flag.threshold,
                "detail": flag.detail,
            }
            for flag in risk.flags
        ],
        "waived": [flag.label for flag in risk.waived],
    }


@router.get("/fundamentals/risk")
def fundamentals_risk_view() -> dict[str, Any]:
    """持仓 ∪ 自选的基本面红旗：季报有没有暴雷迹象。

    「黄金坑、地量战法的前提是基本面没坏」在这里核对：红旗多、score 高的
    票，任何「坑」都要先怀疑是雷。没有季报数据时 400 给采集命令——「没查」
    和「没红旗」是两回事。未覆盖的票返回 level=未知，不编一个「低风险」。
    """
    settings = load_settings()
    symbols = _watch_symbols(settings)
    etf_count = _etf_watch_count(settings)
    if not symbols:
        if etf_count:
            return {
                "available": False,
                "reason": f"自选/持仓只有 {etf_count} 只 ETF——它们没有财报红旗，股票红旗核对轮不到它们；先加一只股票",
            }
        return {"available": False, "reason": "持仓与自选都是空的：先加一只票再核对基本面"}
    panel = fundamentals_fetch.load(settings.data.directory)
    if panel.frame.empty:
        raise HTTPException(
            status_code=400,
            detail="还没有季报数据。点「采集季报」或运行 quant-fundamentals，再回来核对。",
        )
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})
    risks = fundamentals_risk.assess_many(panel, symbols)
    covered = {row.symbol for row in risks if row.level != fundamentals_risk.LEVEL_UNKNOWN}
    return {
        "available": True,
        "n_symbols": len(symbols),
        "n_covered": len(covered),
        "etf_skipped": etf_count,  # 豁免而不是缺失：前端如实标注
        "risks": [_risk_to_dict(row, name_of) for row in risks],
    }


@router.post("/fundamentals/refresh")
def fundamentals_refresh() -> dict[str, Any]:
    """联网采集持仓 ∪ 自选的季报并落盘（每票每表每季一次请求）。"""
    settings = load_settings()
    symbols = _watch_symbols(settings)
    if not symbols:
        raise HTTPException(status_code=400, detail="持仓与自选都是空的：先加一只票")
    panel = fundamentals_fetch.collect(symbols)
    try:
        old = fundamentals_fetch.load(settings.data.directory)
    except FileNotFoundError:
        old = None
    merged = fundamentals_fetch.merge(old, panel) if old is not None else panel
    path = fundamentals_fetch.store(merged, settings.data.directory)
    risks = fundamentals_risk.assess_many(merged, symbols)
    names = universe.load(settings.data.directory)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    # ETF 不在股票 universe 里：名称来自写死的清单，透明且无需联网
    name_of.update({normalize_symbol(k): v for k, v in etf_mod.ETF_UNIVERSE.items()})
    return {
        "available": True,
        "saved": str(path),
        "n_symbols": len(symbols),
        "risks": [_risk_to_dict(row, name_of) for row in risks],
    }


# --- 大事件 -------------------------------------------------------------------


@router.get("/events")
def recent_events(days: int = Query(default=10, ge=2, le=30)) -> dict[str, Any]:
    """最近大事件：从行情本身检测，不假装知道新闻。

    普涨普跌、板块集体异动、个股暴涨暴跌、创阶段新高/新低——这四类是面板
    数据里可验证的全部。没有新闻源就不接新闻，事件列表标明样本范围。
    """
    settings = load_settings()
    try:
        panel = _load_panel(settings.collection.provider, settings.collection.adjust)
    except HTTPException as exc:
        return {"days": days, "events": [], "sector_table": False, "unavailable": str(exc.detail)}
    sector_map, _ = _sector_map(settings)
    return {
        "days": days,
        "as_of": str(panel.prices.index[-1].date()),
        "n_symbols": int(panel.prices.shape[1]),
        "sector_table": bool(sector_map),
        "events": detect_events(panel, sector_map, days=days),
    }
