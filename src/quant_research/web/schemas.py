"""Request and response contracts for the web API.

These models are the whole contract between the browser and the research library.
They deliberately carry *inputs and serialised outputs only* — every number in a
response was produced by ``quant_research`` itself, never recomputed here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

WeightMethod = Literal["fixed", "equal_weight", "inverse_volatility"]
ExecPrice = Literal["close", "open"]


class CostsIn(BaseModel):
    commission_bps: float = 5.0
    slippage_bps: float = 2.0


class RebalanceIn(BaseModel):
    buffer: float = 0.0
    min_trade_notional: float = 0.0


class PolicyIn(BaseModel):
    """Portfolio limits. ``None`` means "do not constrain this dimension"."""

    allow_short: bool = False
    max_weight: float | None = None
    max_gross: float | None = None
    target_net: float | None = None


class BacktestRequest(BaseModel):
    dataset: str
    symbols: list[str] | None = None
    start: str | None = None
    end: str | None = None
    strategy: str = "ma_cross"
    params: dict[str, int | float] = Field(default_factory=dict)
    weight_method: WeightMethod = "equal_weight"
    target_exposure: float = 1.0
    risk_window: int = 20
    exec_price: ExecPrice = "close"
    initial_cash: float = 100_000.0
    # Optional, not default_factory: omitting a field and passing null for it
    # should mean the same thing, which is what every client here does.
    costs: CostsIn | None = None
    rebalance: RebalanceIn | None = None
    policy: PolicyIn | None = None
    max_points: int = 600
    trade_limit: int = 100


class VariantIn(BaseModel):
    """One named deviation from the baseline. Nothing here is auto-generated —
    a grid search UI would make over-fitting one click away."""

    label: str
    params: dict[str, int | float] | None = None
    weight_method: WeightMethod | None = None
    exec_price: ExecPrice | None = None
    costs: CostsIn | None = None
    rebalance: RebalanceIn | None = None
    policy: PolicyIn | None = None
    target_exposure: float | None = None


class CompareRequest(BaseModel):
    base: BacktestRequest
    variants: list[VariantIn] = Field(default_factory=list)


class CollectRequest(BaseModel):
    provider: str = "akshare"
    symbols: list[str] = Field(default_factory=list)
    start: str | None = None
    end: str | None = None
    full: bool = False
    dry_run: bool = False
    lookback_days: int | None = None
    pause: float = 0.0
    retries: int = 1
    # Named all_symbols rather than `all`: a pydantic field called `all` shadows
    # the builtin, and reads oddly at every call site.
    all_symbols: bool = False


class DatasetOut(BaseModel):
    key: str
    label: str
    kind: Literal["sample", "collected"]
    path: str
    provider: str | None = None
    n_symbols: int | None = None
    updated_at: str | None = None


class BacktestOut(BaseModel):
    dataset: str
    meta: dict[str, Any]
    config: dict[str, Any]
    # ``None`` marks a metric that could not be computed (too few observations).
    metrics: dict[str, Any]
    equity: list[list[Any]]
    drawdown: list[list[Any]]
    benchmark: list[list[Any]]
    turnover: list[list[Any]]
    trades: list[dict[str, Any]]
    trade_count: int
    final_equity: float
    total_costs: float
    mean_turnover: float
    warnings: list[dict[str, str]]
    truncated: bool


class ExperimentOut(BaseModel):
    """One line of the ledger: the configuration and what it returned."""

    id: str
    created_at: str
    dataset: str
    strategy: str
    metrics: dict[str, Any] = {}
    final_equity: float = 0.0
    trade_count: int = 0
    total_costs: float = 0.0
    params: dict[str, Any] = {}
    weight_method: str | None = None
    rebalance: dict[str, Any] | None = None
    job_id: str | None = None
    label: str = ""
    note: str = ""


class ExperimentNoteIn(BaseModel):
    label: str | None = None
    note: str | None = None


class GridRequest(BaseModel):
    """One parameter sweep: every combination of ``params`` is run and ranked."""

    dataset: str
    strategy: str
    params: dict[str, list[Any]] = {}
    metric: str = "sharpe"
    weight_methods: list[WeightMethod] = []
    initial_cash: float = 100_000.0
    costs: CostsIn | None = None
    rebalance: RebalanceIn | None = None


class JobProgress(BaseModel):
    done: int
    total: int
    ratio: float


class JobOut(BaseModel):
    id: str
    kind: Literal["backtest", "compare", "collect", "grid"]
    label: str
    status: Literal["pending", "running", "done", "failed", "cancelled"]
    progress: JobProgress
    message: str = ""
    submitted_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    archive: str | None = None
    # True when restored from disk by an earlier run of the process.
    archived: bool = False
    # Only populated when fetching one job, never in the list. Shape depends on
    # the kind: an object for backtest/collect, a list of rows for compare.
    result: Any | None = None


class JobRef(BaseModel):
    """What a 202 response carries: just enough to start polling."""

    id: str
    kind: str
    label: str
    status: str


class CompareRowOut(BaseModel):
    label: str
    metrics: dict[str, float]
    equity: list[list[Any]]
    final_equity: float
    total_costs: float
    trade_count: int
    mean_turnover: float


__all__ = [
    "BacktestOut",
    "BacktestRequest",
    "CollectRequest",
    "CompareRequest",
    "CompareRowOut",
    "CostsIn",
    "DatasetOut",
    "PolicyIn",
    "RebalanceIn",
    "VariantIn",
]
