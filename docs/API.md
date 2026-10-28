# API 参考

本文档描述 `quant_research` 当前的公开接口。除回测引擎持有账本状态外，其余函数均为纯函数。

版本：v0.2.0

---

## `quant_research.config`

模块职责：把 `config/settings.yaml` 读成冻结 dataclass。未知键与非法值都会报错——拼错 `commision_bps` 不允许静默变成「零成本回测」。

### `load_settings`

```python
load_settings(path: str | Path | None = None) -> Settings
```

| 类型 | 字段 |
| --- | --- |
| `DataSettings` | `directory: Path`、`timezone: str = "UTC"`、`price_adjustment: str = "adjusted"` |
| `BacktestSettings` | `initial_cash: float = 100000`、`commission_bps: float = 5`、`slippage_bps: float = 2`、`allow_short: bool = False` |
| `Settings` | `data: DataSettings`、`backtest: BacktestSettings`，另有 `to_dict()` |

**异常**：文件不存在 → `FileNotFoundError`；缺段/未知键/非法值 → `ValueError`；段类型不是映射 → `TypeError`。

`price_adjustment` 只接受 `adjusted` / `forward` / `backward` / `none`。

---

## `quant_research.data`

### `load_close_prices`

```python
load_close_prices(
    path: str | Path,
    date_column: str = "date",
    price_column: str = "close",
) -> pd.Series
```

单标的 CSV → 升序 Series。返回前完成四项校验（缺列、重复日期、缺失值、非正价格），失败抛 `ValueError`。

### `load_price_panel`

```python
load_price_panel(
    path: str | Path | None = None,
    *,
    symbols: Sequence[str] | None = None,
    date_column: str = "date",
    symbol_column: str = "symbol",
    price_column: str = "close",
    start: str | None = None,
    end: str | None = None,
    missing_policy: str = "raise",
    meta: DataMeta | None = None,
) -> PricePanel
```

把 CSV 读成「日期 × 标的」面板。支持三种输入布局：

| 输入 | 说明 |
| --- | --- |
| 长表 CSV（含 `symbol` 列） | 按 `symbol` 透视成面板 |
| 单标的 CSV（无 `symbol` 列） | 标的取 `symbols[0]` 或文件名 |
| 目录 | `data/raw/*.csv`，每个文件一个标的 |

`path=None` 时读取 `settings.data.directory / "raw"`。

**返回**：`PricePanel`

| 成员 | 说明 |
| --- | --- |
| `prices` | `DataFrame(date × symbol)` 收盘价 |
| `meta` | `DataMeta`：来源、区间、时区、复权方式、频率、缺失策略、标的列表 |
| `extra` | `open` / `high` / `low` / `volume` 面板（若源文件含这些列） |
| `field(name)` | 取字段面板；缺失字段抛 `KeyError` |
| `has_field(name)` | 字段是否可用 |

`missing_policy="raise"`（默认）拒绝缺失价格；`"keep"` 保留 `NaN` 交给质量报告处理。

### `describe_panel`

```python
describe_panel(panel: PricePanel, jump_threshold: float = 0.5) -> QualityReport
```

输出样本区间、观测数、每标的缺失率与非正价格数、单日涨跌超过阈值的跳变点（疑似未复权/拆股）、日期单调性。只报告，不修复。`QualityReport` 提供 `to_frame()`、`to_dict()` 与 `is_clean`。

---

## `quant_research.strategies`

### 信号契约

```python
SignalFn = Callable[..., pd.DataFrame]
```

输入价格面板，输出 `date × symbol` 的**原始分数**，**不做滞后**。滞后统一由回测引擎执行一次。

| 接口 | 说明 |
| --- | --- |
| `STRATEGY_REGISTRY` | `{name: factory}`，内置 `ma_cross`、`random` |
| `get_signal(name)` | 取工厂函数；未注册抛 `KeyError` |
| `register(name)` | 装饰器，重复注册抛 `ValueError` |
| `apply_lag(frame, periods=1)` | 显式滞后工具；`periods < 0` 抛 `ValueError` |

### `moving_average_signal`（注册名 `ma_cross`）

```python
moving_average_signal(
    close: pd.Series | pd.DataFrame,
    fast_window: int = 20,
    slow_window: int = 50,
    min_periods: int | None = None,
) -> pd.Series | pd.DataFrame
```

快均线高于慢均线 → 1，否则 0，dtype `int8`。DataFrame 输入逐列计算。`min_periods` 默认等于 `slow_window`（窗口未满不出信号）。

### `random_signal`（注册名 `random`）

```python
random_signal(close, probability: float = 0.5, seed: int = 0)
```

对照用随机 0/1 信号。用于验证「策略是否真的优于随机」。

---

## `quant_research.web`

本地工作台的 HTTP 契约（设计与取舍见 [UI_DESIGN.md](UI_DESIGN.md)，可选依赖 `pip install -e '.[web]'`）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 存活、已注册策略、数据目录 |
| GET | `/api/datasets` | 数据集清单 |
| GET | `/api/datasets/{key}/overview` | 元信息 + 质量报告 + 走势 + 预览 |
| POST | `/api/backtest` | 运行一次回测 |
| POST | `/api/backtest/compare` | 基线 + 若干变体并排 |
| GET | `/api/collection/status` | 各 provider 落盘状态与失败清单 |
| GET | `/api/collection/reports` | 归档的采集报告，倒序列出 |
| POST | `/api/collection/collect` | 触发一次采集（`dry_run` 可试运行） |
| POST | `/api/jobs/{backtest,compare,collect,grid}` | 提交后台任务，立即返回 202 + job id |
| GET | `/api/jobs`、`/api/jobs/{id}` | 任务列表 / 单个任务的状态、进度与结果 |
| DELETE | `/api/jobs/{id}` | 请求取消一个尚未结束的任务 |
| GET | `/api/experiments` | 实验台账（可按指标排序、按策略筛选） |
| PATCH | `/api/experiments/{id}` | 给一条实验加标签或备注 |
| DELETE | `/api/experiments/{id}` | 删除一条实验记录 |

慢路径（全市场回测、批量补数）应走 `/api/jobs/...`：提交后轮询 `GET /api/jobs/{id}`，
`progress` 给出 `done/total`，`status` 到达 `done` 后 `result` 即为同步端点的同构响应。
结果同时归档到 `reports/webui/<id>_<kind>.json`。

三条约束，避免界面成为第二个真相来源：

1. 数值全部来自库函数（`run_backtest` / `summarize` / `describe_panel`），路由只做编排与序列化；
2. 数据集用键寻址（`sample:<stem>`、`raw:<provider>`），不接受任意路径；
3. 序列超过 `max_points` 时等间隔抽样，响应体大小有上界。

## `quant_research.collector`

```python
collect_once(
    *,
    provider: MarketDataProvider,
    store: LocalMarketStore,
    symbols: Sequence[str],
    start: date | None = None,
    end: date | None = None,
    full: bool = False,
    lookback_days: int = 10,
    calendar: TradingCalendar | None = None,
    retries: int = 2,
    backoff: float = 2.0,
    pause: float = 0.0,
    timezone: str = "Asia/Shanghai",
    dry_run: bool = False,
) -> CollectionReport
```

单次收集：逐标的请求、合并入本地库。**单个标的失败只进 `report.failed`，不影响其它标的**；
`report.failed` 非空时不会把当天标记为已完成，便于重试。

`lookback_days` 每次都重读一段尾部窗口——前复权价格会在分红后回溯改写，重读尾部能让近期历史与
供应商口径保持一致，而不必全量重写。

`run_daily_loop(collect, *, at, timezone, calendar, tick_seconds=60, max_runs=None)`：
到点 + 交易日 + 当天未成功才触发；单次异常被捕获，守护进程继续运行。

### 数据源

| provider | 说明 |
| --- | --- |
| `akshare` | A 股日线（东方财富口径），可选依赖：`pip install -e '.[collect]'` |
| `baostock` | A 股日线（BaoStock），免费无 token；成交量单位为股，已换算为手 |
| `sse` | 沪市官方证券清单（权威、免费）；**不提供行情**，`fetch_daily` 报错 |
| `mock` | 确定性合成行情，离线可用，用于测试与演示 |

`get_provider(name, **kwargs)` 按名构造；`normalize_symbol("600519")` → `600519.SH`（自动推断交易所）。
provider 返回**标准 bar 帧**：日期索引 + `open/high/low/close/volume` 必需列，单位为源单位（A 股为「手」）。

### 存储

```python
LocalMarketStore(root, provider, fmt="parquet")
```

| 方法 | 说明 |
| --- | --- |
| `upsert(symbol, bars)` | 按日期去重合并（新值覆盖旧值），原子写，返回 `UpsertResult` |
| `read(symbol)` / `stored_symbols()` | 读回单个标的 / 列出已存标的 |
| `load_state()` / `save_state()` | `_state.json` 往返，损坏时会重建而不是崩溃 |

无 `pyarrow` 时自动降级为 CSV，并清理同名旧格式文件。

## `quant_research.portfolio`

信号**组合内的相对大小**与**允许持有多少**是两件事，本包把它们分开：

- `signal_to_weights` 决定每个名字该拿多少；
- `PortfolioPolicy` 决定什么是被允许的（单标的上限、总杠杆、净敞口），只用确定性缩放实现，**不是优化器**。

执行层面的控制（何时下单）在 `backtest.RebalancePolicy`，因为它需要实时账本才能回答。

### `signal_to_weights`

```python
signal_to_weights(
    signal: pd.Series | pd.DataFrame,
    *,
    method: Literal["fixed", "equal_weight", "inverse_volatility"] = "fixed",
    target_exposure: float = 1.0,
    allow_short: bool = False,
    risk: pd.Series | pd.DataFrame | None = None,
    policy: PortfolioPolicy | None = None,
) -> pd.DataFrame
```

| 方法 | 行为 |
| --- | --- |
| `fixed` | 正分数 → `target_exposure`，其余 0（适用于 0/1 信号） |
| `equal_weight` | 每期在有信号的标的上等分 `target_exposure` |
| `inverse_volatility` | 按 `1 / risk` 分配；`risk` 缺失时退化为等权，无可用风险估计的标的不分配 |

传入 `policy` 时会对结果施加约束；`policy.allow_short=True` 会覆盖 `allow_short` 参数。

`signal_to_weights_with_policy(..., policy=...)` 返回 `WeightResult`，额外给出约束统计。

### `PortfolioPolicy` / `enforce_policy`

```python
PortfolioPolicy(
    allow_short: bool = False,
    max_weight: float = 1.0,          # 单标的上限（绝对值）
    max_gross: float = 1.0,           # sum(|w|) 硬上限
    target_net: float | None = None,  # sum(w) 目标
    scaling: Literal["water_fill", "proportional"] = "water_fill",
)

enforce_policy(weights: pd.DataFrame, policy: PortfolioPolicy) -> PolicyResult
```

`PolicyResult` 含 `weights` 与 `stats`（`short_dropped` / `weight_capped` / `gross_scaled` / `net_adjusted`，各为触发天数）。

**求解方式**：逐日独立处理，先把总权重归一化到目标，再对单标的超额部分按相对比例重新分配（水填充），而不是整体缩放——后者会把无处安放的敞口无声地丢掉。多空账簿用解析解 `(L+S=gross, L-S=net)` 缩放两个方向。

**性质**：确定性、幂等（第二次施加是无操作）、不使用协方差或预测。约束确实会在触碰时牺牲收益，这是有意的——它是"可交易性"的定价。

**限制**：只处理单标的、总杠杆与净敞口；没有行业/板块约束，也不做风险最小化。

---

## `quant_research.backtest`

### 时序契约

| 时刻 | 事件 |
| --- | --- |
| t 收盘后 | `signal[t]` → `target_weights[t]` |
| t+lag | 以执行价把仓位调到 `target_weights[t]` |
| t 收盘 | 按收盘价估值，得到 `equity[t]` |

`price="close"` 时执行价为**前一 bar 收盘价**（决策时已知，无前视）；`open` / `vwap` 时执行价落在 bar t 内，需显式传入对应面板。

### 配置

```python
CostConfig(commission_bps=5.0, slippage_bps=2.0, min_commission=0.0)

ExecutionConfig(
    price="close",  # close | open | vwap
    lag=1,  # 决策到成交的 bar 数
    sizing="exec_price",  # exec_price | prev_close
    integer_shares=False,
    lot_size=1,
    clip_to_cash=True,
)

BacktestConfig(
    initial_cash=100_000.0,
    costs=CostConfig(),
    execution=ExecutionConfig(),
    rebalance=RebalancePolicy(),  # buffer / min_trade_notional
    allow_short=False,
    accounting="shares",  # shares | weights
)
```

`BacktestConfig.from_settings(settings, **overrides)` 从 YAML 配置构造，`overrides` 可覆盖任意字段（如 `execution=ExecutionConfig(price="open")`）。

### `run_backtest`

```python
run_backtest(
    prices: pd.DataFrame,
    target_weights: pd.DataFrame,
    *,
    exec_prices: pd.DataFrame | None = None,
    config: BacktestConfig | None = None,
    meta: DataMeta | None = None,
) -> BacktestResult
```

`target_weights[t]` 表示 t 收盘后想要的目标仓位；引擎内部按 `lag` 滞后执行。

### `run_signal_backtest`

```python
run_signal_backtest(
    prices,
    signal,
    *,
    target_exposure: float = 1.0,
    weight_method: str = "fixed",
    risk: pd.Series | pd.DataFrame | None = None,
    policy: PortfolioPolicy | None = None,
    exec_prices: pd.DataFrame | None = None,
    config: BacktestConfig | None = None,
    meta: DataMeta | None = None,
) -> BacktestResult
```

`signal_to_weights` → `run_backtest` 的封装，是推荐入口：调用方无法绕过滞后处理。
`policy` 在仓位换算阶段施加组合约束；`risk` 供 `inverse_volatility` 使用。

### `RebalancePolicy`

```python
RebalancePolicy(buffer: float = 0.0, min_trade_notional: float = 0.0)
```

| 字段 | 说明 |
| --- | --- |
| `buffer` | 目标权重与**当前实际权重**（随价格漂移）之差不超过此值时跳过该标的。首次建仓与清仓不受影响 |
| `min_trade_notional` | 单笔名义成交额低于此金额则不下单 |

两者都需要实时账本，因此只在 `accounting="shares"` 生效（`weights` 近似模式会给出提示）。默认值 0 表示"每次权重变化都交易"，行为与之前一致。

**注意**：换手控制不保证提升收益，只保证少付不必要的成本。样例数据（等权、每日再平衡）上
`buffer=0.05 + min_trade_notional=1000` 把交易笔数从 703 降到 70，成本下降 9%，夏普仅从 0.695 降到 0.669。

### `BacktestResult`

| 字段 | 说明 |
| --- | --- |
| `equity` / `returns` | 每日净值与收益率 |
| `weights` | 每日收盘实际权重（shares 模式，含价格漂移） |
| `positions` | 每日持股数 |
| `trades` | `date, symbol, shares, price, fill_price, gross, commission, slippage, cost, reason` |
| `turnover` | 每日单边成交额 / 前一日净值 |
| `costs` | 每日 `commission` / `slippage` / `total` |
| `cash` | 每日现金（shares 模式） |
| `warnings` | `(kind, date, symbol)`，如 `skipped_symbol`、`cash_clipped`、`missing_meta` |
| `summary(benchmark=None)` | 委托 `metrics.summarize` |

`accounting="weights"` 是向量化近似（无交易流水与股数），用于快速筛查与交叉校验：零成本下必须与 `shares` 模式逐点一致。

### 成交与成本

```text
exec_px      = price × (1 ± slippage_bps/10000)     # 买高卖低
commission   = |Δshares| × exec_px × commission_bps/10000
slippage     = |Δshares| × price × slippage_bps/10000
cash        -= Δshares × exec_px + commission
```

同一 bar 内**先卖后买**，卖出释放的现金供买入使用。买入预算已预留成本；现金仍不足时按可用现金缩量，记 `cash_clipped`。

---

## `quant_research.metrics`

| 函数 | 说明 |
| --- | --- |
| `annualized_sharpe(returns, periods_per_year=252)` | 年化夏普，无风险利率取 0；波动为 0 时返回 0 |
| `max_drawdown(returns)` | 最大回撤（负数）；任一收益 ≤ -100% 抛 `ValueError` |
| `cumulative_return` / `cagr` / `annualized_volatility` | 累计收益、年化收益、年化波动 |
| `downside_deviation` / `sortino_ratio` / `calmar_ratio` | 下行风险与风险调整收益 |
| `drawdown_series` / `drawdown_table(top=5)` | 水下曲线；回撤区间表（起点、谷底、恢复、深度、时长） |
| `win_rate` / `profit_factor` | 按**周期收益**统计的胜率与盈亏比 |
| `exposure(weights)` | 平均总敞口 |
| `benchmark_buy_hold(prices, symbol=None)` | 等权买入持有基准 |
| `relative_return` / `alpha_beta` / `tracking_error` / `information_ratio` | 相对与主动管理指标 |
| `summarize(returns, benchmark=None)` | 一张指标表；含基准时附 `alpha`、`beta`、`tracking_error`、`information_ratio` |

`drawdown_table` 的 `recovered` 表示区间结束后是否回到前高；`recovery_days` 为谷底到恢复日的天数，未恢复为 `NA`。

---

## 测试覆盖现状

| 模块 | 测试 |
| --- | --- |
| `config` | ✅ 未知键、缺段、非法值、不可变、`to_dict` |
| `data` | ✅ 四类校验、长表/目录加载、符号与日期过滤、缺失策略、质量报告 |
| `strategies` | ✅ 单/多标的、注册表、随机信号、滞后 |
| `portfolio` | ✅ 三种权重方法、敞口缩放、负分数；约束求解（上限/杠杆/净敞口/多空）与幂等性 |
| `backtest` | ✅ 手算一致、成本单调、因果性、lag、双账本一致、现金非负、换手、缺失价格、空信号、缓冲带、最小成交额 |
| `metrics` | ✅ 边界与已知样本、回撤表、基准对照、`summarize` |

## 相关文档

- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — 近期批次与细化设计
- [ROADMAP.md](ROADMAP.md) — 分期建设路径
- [ARCHITECTURE.md](ARCHITECTURE.md) — 数据流与分层设计
- [DATA.md](DATA.md) — 数据格式规范
- [CONVENTIONS.md](CONVENTIONS.md) — 研究约定与回测防坑清单
