"""Request and response contracts for the web API.

These models are the whole contract between the browser and the research library.
They deliberately carry *inputs and serialised outputs only* — every number in a
response was produced by ``quant_research`` itself, never recomputed here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator

from quant_research.data.providers.base import normalize_symbol


def _canonical_symbols(value: list[str] | None) -> list[str] | None:
    """Bare codes are fine: the digits decide the exchange, so ``600519`` is
    ``600519.SH`` and ``000001`` is ``000001.SZ`` without anyone typing a dot."""
    if value is None:
        return None
    out: list[str] = []
    for item in value:
        try:
            out.append(normalize_symbol(item))
        except ValueError as exc:
            raise ValueError(f"无法识别的代码 {item!r}：{exc}") from exc
    return out


CanonicalSymbols = Annotated[list[str], BeforeValidator(_canonical_symbols)]


def _canonical_symbol(value: str) -> str:
    """Single-code twin of ``_canonical_symbols``: same tolerance, same 422."""
    try:
        return normalize_symbol(value)
    except ValueError as exc:
        raise ValueError(f"无法识别的代码 {value!r}：{exc}") from exc


CanonicalSymbol = Annotated[str, BeforeValidator(_canonical_symbol)]

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
    symbols: CanonicalSymbols | None = None
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
    symbols: CanonicalSymbols = Field(default_factory=list)
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


class ScreenRequest(BaseModel):
    """Rank the market and keep a short list.

    ``as_of`` replays an earlier day; a replay is not written to the ledger,
    because it is not an opinion held that day.
    """

    provider: str = "baostock"
    adjust: str | None = None
    top_n: int = 10
    min_history: int = 60
    min_amount: float | None = None
    as_of: str | None = None
    record: bool = True
    note: str = ""


class ScreenPickOut(BaseModel):
    symbol: str
    rank: int
    score: float
    weight: float


class ScreenOut(BaseModel):
    """One short list.

    ``as_of`` and ``latest_session`` are both here so the interface can say which
    day the list is about: run before the close and the list is about yesterday,
    which is the one thing people misread from a panel with no date on it.
    """

    as_of: str
    latest_session: str
    provider: str
    universe: int
    picks: list[ScreenPickOut]
    dropped: dict[str, int]
    note: str = ""
    stale: bool = False


class ScreenReviewOut(BaseModel):
    horizon: int
    rows: list[dict[str, Any]]
    screens: int = 0
    mean_excess: float | None = None
    beat_market: float | None = None


class IndicatorRowOut(BaseModel):
    """One indicator, described and summarised for a table.

    ``median`` and the range are the last session's cross-section, not a single
    value: an indicator says something per name, and collapsing that to one
    number would suggest it says one thing about the market.
    """

    field: str
    label: str
    unit: str
    typical_range: str
    description: str
    how_to_use: str
    caution: str
    median: float | None = None
    low: float | None = None
    high: float | None = None
    symbols: int = 0


class IndicatorBoardOut(BaseModel):
    """Every indicator plus the text that explains it, from one catalogue."""

    as_of: str
    provider: str
    adjust: str
    symbols: int
    fields: int
    params: dict[str, Any]
    rows: list[IndicatorRowOut]
    # true when the stored indicators are older than the price panel
    stale: bool = False


class IndicatorValueOut(BaseModel):
    field: str
    label: str
    unit: str
    value: float | None = None


class IndicatorDetailOut(BaseModel):
    """One name's indicator values — the numbers behind the cross-section."""

    symbol: str
    as_of: str
    provider: str
    sector: str | None = None
    values: list[IndicatorValueOut]


class SeriesLineOut(BaseModel):
    """One line in a chart: a field name, a label, and the points to draw."""

    field: str
    label: str
    # "bar" for MACD's histogram: the sign is the point, a line hides it
    kind: Literal["line", "bar"] = "line"
    # [[date, value]]; a null value is a gap, not a zero
    points: list[list[Any]]


class SeriesPanelOut(BaseModel):
    key: str
    title: str
    unit: str
    # horizontal guides, e.g. 20/80 for RSI — the thresholds readers remember
    reference: list[float] = Field(default_factory=list)
    lines: list[SeriesLineOut]
    # ``[date, open, high, low, close]`` rows for the price panel; empty for the
    # oscillator panels, which have no candle semantics
    candles: list[list[Any]] = Field(default_factory=list)


class IndicatorSeriesOut(BaseModel):
    """A name's indicator history, split into panels a chart can draw alone."""

    symbol: str
    as_of: str
    provider: str
    days: int
    panels: list[SeriesPanelOut]


class JobProgress(BaseModel):
    done: int
    total: int
    ratio: float


class JobOut(BaseModel):
    id: str
    kind: Literal["backtest", "compare", "collect", "grid", "screen"]
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


class SymbolOut(BaseModel):
    """One autocomplete hit: the code to submit, and the name to read."""

    symbol: str
    name: str = ""


class SymbolSearchOut(BaseModel):
    """Hits plus whether the name table exists at all.

    ``available=False`` is not an error: the inputs still accept a code, they
    just cannot complete it. Saying so lets the UI prompt for ``quant-symbols
    --refresh`` instead of quietly showing nothing.
    """

    items: list[SymbolOut] = Field(default_factory=list)
    available: bool = False
    total: int = 0
    updated_at: str | None = None


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
    "SymbolOut",
    "SymbolSearchOut",
    "VariantIn",
]


class WatchlistIn(BaseModel):
    """Upsert one stock: categories replace the old set, note only if given."""

    symbol: CanonicalSymbol
    name: str = ""
    categories: list[str] = Field(default_factory=list)
    note: str | None = None


class PositionIn(BaseModel):
    """Upsert one holding: shares and cost must both be positive.

    融券与「零成本」这里都表达不了，与其猜，不如让请求失败得明明白白。
    """

    symbol: CanonicalSymbol
    shares: float = Field(gt=0, description="持股数（股）")
    cost: float = Field(gt=0, description="成本价（元）")
    note: str | None = None
    account: str | None = Field(
        default=None,
        description="账户名（不同券商分开记）；缺省为「默认」账户",
        max_length=24,
    )


class CategoryIn(BaseModel):
    """A group name; whitespace-only is rejected, not silently kept."""

    name: str = Field(min_length=1, max_length=30)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("分类名不能为空白")
        return value


__all__ = [
    "BacktestOut",
    "BacktestRequest",
    "CategoryIn",
    "CollectRequest",
    "CompareRequest",
    "CompareRowOut",
    "CostsIn",
    "DatasetOut",
    "PolicyIn",
    "RebalanceIn",
    "SymbolOut",
    "SymbolSearchOut",
    "VariantIn",
    "WatchlistIn",
]
