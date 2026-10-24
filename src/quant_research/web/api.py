"""HTTP routes: thin orchestration over the research library.

Every number returned here was computed by ``quant_research`` itself. This module
validates input, calls library functions and serialises the result — it contains no
pricing logic of its own, so the UI can never disagree with a notebook.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException

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
from quant_research.data import describe_panel, load_price_panel
from quant_research.data.calendar import TradingCalendar
from quant_research.data.providers import get_provider
from quant_research.data.store import LocalMarketStore
from quant_research.logging_setup import get_logger
from quant_research.metrics import benchmark_buy_hold, drawdown_series
from quant_research.strategies import STRATEGY_REGISTRY, get_signal

from .experiments import ExperimentStore
from .experiments import get_store as get_experiments
from .grid import MAX_COMBINATIONS, expand, rank
from .jobs import JobStore, Progress, get_store
from .schemas import (
    BacktestOut,
    BacktestRequest,
    CollectRequest,
    CompareRequest,
    CompareRowOut,
    CostsIn,
    DatasetOut,
    ExperimentNoteIn,
    GridRequest,
    JobOut,
    JobRef,
    PolicyIn,
    RebalanceIn,
    VariantIn,
)

logger = get_logger("web.api")

router = APIRouter(prefix="/api")


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
    signal = get_signal(request.strategy)(panel.prices, **request.params)

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


def _collect_report(
    request: CollectRequest,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    try:
        provider = get_provider(request.provider, adjust=settings.collection.adjust)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = LocalMarketStore(
        settings.collection.output_directory,
        provider.name,
        fmt=settings.collection.format,
    )
    symbols = request.symbols or store.stored_symbols()
    if not symbols:
        raise HTTPException(status_code=400, detail="no symbols supplied and none are stored yet")

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
            pause=request.pause,
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
    return report.to_dict()


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
        get_provider(request.provider, adjust=settings.collection.adjust)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    market = LocalMarketStore(
        settings.collection.output_directory, request.provider, fmt=settings.collection.format
    )
    symbols = request.symbols or market.stored_symbols()
    if not symbols:
        raise HTTPException(status_code=400, detail="no symbols supplied and none are stored yet")

    def task(progress: Progress, stop: Callable[[], bool]) -> dict[str, Any]:
        def on_progress(done: int, total: int, symbol: str) -> None:
            progress.update(done, f"{symbol or '完成'}（{done}/{total}）")

        return _collect_report(request, on_progress=on_progress, should_stop=stop)

    job = jobs.submit(
        "collect", f"{request.provider} · {len(symbols)} 标的", task, total=len(symbols)
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
