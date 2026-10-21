# 近期实施计划与细化设计

> **落地状态（v0.2.0，2026-10-21）**：批次 A~E 已实现，`pytest` 69 条通过、`ruff` 与 `mypy` 无告警。
> 实现期相对本文档的偏离记录在文末[「落地记录」](#十三落地记录v020)。

本文档把 [IMPLEMENTATION_BRIEF.md](IMPLEMENTATION_BRIEF.md) 第五节「建议实施路径」的**第 1~5 步**拆成近期可执行的任务，并细化到接口、数据结构、时序与测试级别。

- **上游**：[IMPLEMENTATION_BRIEF.md](IMPLEMENTATION_BRIEF.md)（为什么做、做到什么程度）
- **下游**：[ROADMAP.md](ROADMAP.md)（完整阶段任务清单）、[ARCHITECTURE.md](ARCHITECTURE.md)（分层与依赖）
- **边界**：本文件只覆盖「工程底座 → 数据/配置契约 → 信号与仓位契约 → 最小回测引擎 → 最小评估门槛」。第 6~8 步（策略族扩展、实验归档与报告、纸面验证）**不在近期范围**，见文末「明确不做」。

---

## 一、现状勘误（相对 ROADMAP 的实际状态）

开工前先校正几处与代码不符的描述，避免按过期信息排期：

| ROADMAP 描述 | 实际状态 | 影响 |
| --- | --- | --- |
| 「当前目录尚未 `git init`」 | 已 `git init`，但 `master` 分支**无提交**，全部文件处于未跟踪 | P0 第一项改为「首次提交」，不是「初始化仓库」 |
| 「`settings.yaml` 无人读取」 | `config.py::load_settings` 已实现并被 `tests/test_config.py` 覆盖 | 缺的不是「加载器」，而是**类型化与默认值** |
| 「三个模块彼此零依赖」 | 仍成立 | — |
| 「`.gitkeep` 缺失」 | 仍缺失（4 个） | 空目录不入库，CI 克隆后目录消失 |
| 测试覆盖 | 仅 `test_config.py`、`test_momentum.py`；`data.py`、`metrics.py` 覆盖为 0 | 指标层 0 测试却在承担结论输出，风险高 |

---

## 二、近期目标与验收总门槛

**一句话目标**：让「一条均线策略 + 一份本地 CSV」在一条命令内产出**可与手算逐笔核对**的净值曲线、交易流水与核心指标。

**总门槛（v0.4）**：

1. 新环境 `pip install -e '.[dev]' && pytest` 全绿，且 `ruff check` 无告警。
2. 存在内置样例数据，README 的端到端示例**离线可跑**。
3. 零成本回测结果与手算逐笔一致（误差 < 1e-9）；开启成本后终值不高于零成本终值。
4. 存在前视偏差的自动化断言：篡改未来价格不改变历史净值。
5. 一次运行输出一张指标表，含基准（买入持有）对照。

---

## 三、批次划分

| 批次 | 对应实施路径 | 主题 | 交付物 | 相对投入 |
| --- | --- | --- | --- | --- |
| A | 第 1 步 | 工程底座 | 首次提交、CI、pre-commit、LICENSE/CHANGELOG、日志约定 | 低 |
| B | 第 2 步 | 配置与数据契约 | `Settings` 冻结 dataclass、价格面板、样例数据、质量报告 | 中 |
| C | 第 3 步 | 信号与最小仓位契约 | `SignalFn` 协议、注册表、`apply_lag`、固定仓位 | 低 |
| D | 第 4 步 | **最小回测引擎** | 撮合循环、成本模型、账本、交易流水 | 中 |
| E | 第 5 步（最小集） | 评估门槛 | 指标扩展、基准对照、指标层补测试 | 低 |

**执行原则**：A 可独立先做；B→C→D 串行（D 依赖 B 的面板形态与 C 的权重契约）；E 在 D 产出净值曲线后才有意义。每批次结束必须满足自己的 DoD，不允许带着未验证的假设进入下一批。

---

## 四、批次 A：工程底座

### A1. 首次提交

- 补齐 4 个占位文件：`data/raw/.gitkeep`、`data/processed/.gitkeep`、`data/cache/.gitkeep`、`reports/.gitkeep`（`.gitignore` 已有对应白名单规则，缺文件本身）。
- 首次提交前确认 `data/raw/` 无任何真实数据文件；`.vscode/`、`quant-trading.code-workspace` 一并入库（当前为未跟踪状态，属工程配置，不含凭据）。

### A2. CI（GitHub Actions）

`.github/workflows/ci.yml`，矩阵 `python-version: ["3.11", "3.12"]`，步骤：`pip install -e '.[dev]'` → `ruff check .` → `pytest`。暂不加覆盖率门槛，先保证「红就是红」。

### A3. 代码规范与类型

- `pre-commit`：仅挂 `ruff format` + `ruff check --fix`，不引入过多钩子。
- 类型检查：**渐进式**。先在 `pyproject.toml` 配置 `mypy`，只对新增的 `backtest/`、`portfolio/` 目录强制，存量模块用 `# type: ignore` 或暂不纳入 `files`。理由：一次性给存量加类型会稀释本批次的重点。

### A4. 日志约定

- 新增 `quant_research/logging_setup.py`，提供 `get_logger(__name__)`；模块内**不得** `print`。
- 日志只用于「过程可观测」（加载了哪些文件、丢弃了多少行、成本被裁剪），不用于输出结论——结论走返回值与 `reports/`。

### A5. DoD

- `git log` 有首次提交；全新克隆目录执行 `pip install -e '.[dev]' && ruff check . && pytest` 通过。
- `grep -rn "print(" src/` 无结果。

---

## 五、批次 B：配置与数据契约

### B1. `Settings` 冻结 dataclass

现状：`load_settings()` 返回裸 dict（`tests/test_config.py` 按 `settings["data"]["directory"]` 断言）。问题：无默认值、无类型、拼错 key 静默通过——成本参数一旦拼错，回测会「静默无成本」，属于高代价失败。

```python
@dataclass(frozen=True)
class DataSettings:
    directory: Path
    timezone: str = "UTC"
    price_adjustment: str = "adjusted"  # adjusted | forward | backward | none


@dataclass(frozen=True)
class BacktestSettings:
    initial_cash: float = 100_000.0
    commission_bps: float = 5.0
    slippage_bps: float = 2.0
    allow_short: bool = False


@dataclass(frozen=True)
class Settings:
    data: DataSettings
    backtest: BacktestSettings

    def to_dict(self) -> dict[str, Any]: ...
```

**迁移策略**（一次性成本，明确接受）：

- `load_settings()` 返回值改为 `Settings`；未知字段直接 `raise ValueError`（防止拼错静默生效）。
- 同步改写 `tests/test_config.py` 的 3 处下标断言为属性访问，并新增 2 条：未知字段报错、缺失 `backtest` 段报错。

### B2. 价格面板契约

`load_close_prices` 保留（向后兼容，单标的入口），新增面板入口：

```python
def load_price_panel(
    path: str | Path | None = None,
    *,
    symbols: Sequence[str] | None = None,
    fields: Sequence[str] = ("close",),
    start: str | None = None,
    end: str | None = None,
) -> PricePanel
```

- `PricePanel`：`dataclass(prices: DataFrame(date × symbol), meta: DataMeta)`。单标的也返回 `PricePanel`，保证跨层接口统一（ARCHITECTURE 第 2 节约定）。
- `DataMeta`：`source`、`start`、`end`、`timezone`、`price_adjustment`、`frequency`、`missing_policy`、`symbols`。缺元信息的面板不得进入回测（`run_backtest` 校验 `meta` 非空）。
- 目录结构：`src/quant_research/data/` 由单文件改为包，`__init__.py` 重导出 `load_close_prices`，保证现有 import 不断。

**首版只做 CSV**：优先支持「一个文件一个标的（`data/raw/<SYMBOL>.csv`）」与「一个文件含 `symbol` 列」两种布局，按 `symbol` 列拼接后 pivot。Parquet 缓存与 mtime 失效**后置**到多标的批量场景真正出现时再建——当前数据量不足以支撑缓存的复杂度。

### B3. 内置样例数据

- 生成 `data/sample/sample_prices.csv`（合成，3 标的 × 约 500 个交易日，固定随机种子），写入 `data/sample/README.md` 说明「合成数据，仅用于验证管线，不含真实市场信息」。
- `.gitignore` 增加白名单 `!data/sample/**`（否则整个 `data/` 被忽略）。
- README 端到端示例改为读取该样例，保证**离线可跑**。

### B4. 数据质量报告

```python
def describe_panel(panel: PricePanel) -> QualityReport
```

输出：样本区间、标的数、每标的缺失率、非正价格数、单日收益率绝对值超过阈值（默认 50%）的跳变点（疑似未复权/拆股）、日期单调性。返回结构化对象 + `to_frame()`，供后续实验归档复用。首版**只报告不修复**，修复策略留给使用者在配置里显式声明。

### B5. DoD

- `load_settings()` 返回 `Settings`，`pytest` 全绿（含改写后的配置测试）。
- `load_close_prices` 补齐 4 类校验测试（缺列、重复日期、缺失值、非正价格）。
- README 的端到端示例在新环境可直接运行并输出数字。

---

## 六、批次 C：信号与最小仓位契约

### C1. 信号契约

```python
SignalFn = Callable[..., pd.DataFrame]  # 输入面板/价格，输出 date × symbol 分数，未滞后
```

- 约定（沿用 ARCHITECTURE 第 3 节）：信号函数返回**未滞后**原始值，滞后只在回测引擎内发生一次。
- 注册表 `quant_research/strategies/__init__.py::STRATEGY_REGISTRY: dict[str, SignalFactory]`，`get_signal(name, **params)` 取用。首版只注册 `ma_cross`（现有双均线）。
- `apply_lag(df, periods=1)` 作为公共工具显式暴露，但**引擎内部强制调用**，不允许依赖调用方自觉。

### C2. `moving_average_signal` 升级

- 签名改为接受 `pd.Series | pd.DataFrame`，DataFrame 时逐列计算（向量化 rolling，不写循环）。
- 新增 `min_periods` 参数（默认等于 `slow_window`），保持现有「窗口不满则为 0」行为不变，避免破坏 `test_momentum.py`。
- 新增测试：DataFrame 输入逐列等价、列顺序无关、全 NaN 列不产生信号。

### C3. 最小仓位：固定仓位，不做优化器

```python
def signal_to_weights(
    signal: pd.DataFrame,
    *,
    method: Literal["fixed", "equal_weight"] = "fixed",
    target_exposure: float = 1.0,
    allow_short: bool = False,
) -> pd.DataFrame
```

- `fixed`：单标的场景，分数 > 0 → `target_exposure`，否则 0。分数本身不代表仓位大小（双均线是 0/1）。
- `equal_weight`：多标的场景，对每期有信号的标的等权，归一化后乘 `target_exposure`。
- `allow_short=False` 时对所有负权重直接截断为 0，并记录一条 warning（而不是抛错，避免多标的场景因个别标的中断整条链路）。

**近期不做**：波动率倒数、风险平价、行业/换手约束。实施路径第 3 步明确要求「先用固定仓位，不急于开发复杂因子库或优化器」。

### C4. DoD

- 新增一个策略只需新增一个文件并注册，主流程零改动（用一条注册表测试断言：`STRATEGY_REGISTRY` 内每个信号都能对样例面板产出同形 DataFrame）。
- 每个信号至少 1 条正常路径 + 1 条异常路径测试。

---

## 七、批次 D：最小回测引擎（核心）

### D1. 模块划分

```text
src/quant_research/backtest/
├── __init__.py       # run_backtest, run_signal_backtest
├── config.py         # CostConfig, ExecutionConfig, BacktestConfig
├── costs.py          # 手续费/滑点计算（纯函数，易测）
├── engine.py         # 逐 bar 事件循环，产出 BacktestResult
└── ledger.py         # 现金/持仓账本与交易流水
```

### D2. 配置结构

```python
@dataclass(frozen=True)
class CostConfig:
    commission_bps: float = 5.0  # 双边，按成交金额
    slippage_bps: float = 2.0  # 按成交金额方向加减
    min_commission: float = 0.0  # 预留：最低佣金，默认关闭


@dataclass(frozen=True)
class ExecutionConfig:
    price: Literal["close", "open", "vwap"] = "open"  # 默认次日开盘，最保守
    lag: int = 1  # 决策到执行的间隔，默认 1 个 bar
    sizing: Literal["exec_price", "prev_close"] = "exec_price"
    integer_shares: bool = False  # 首版默认连续份额
    clip_to_cash: bool = True  # 现金不足时缩量并告警


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 100_000.0
    costs: CostConfig = CostConfig()
    execution: ExecutionConfig = ExecutionConfig()
    allow_short: bool = False
```

### D3. 时序定义（唯一权威口径）

| 时刻 | 事件 | 可用信息 |
| --- | --- | --- |
| t 日收盘后 | 计算 `signal[t]` → `target_weights[t]` | 截至 t 日（含）的行情 |
| t+1 日 | 以 `exec_price[t+1]` 把仓位调整到 `target_weights[t]` | t+1 的开盘价（或配置的价格字段） |
| t+1 日收盘 | 按收盘价估值，得到 `equity[t+1]`、`portfolio_return[t+1]` | — |

实现上等价于 `exec_weights = target_weights.shift(lag)`。**`shift` 只允许出现在引擎内部的这一处**——这是前视偏差的唯一防线，需用测试锁死。

### D4. 撮合与成本

对第 t 个 bar、标的 s：

```text
prev_equity   = cash + Σ shares[s] * close[t-1]
target_shares = exec_weights[t][s] * prev_equity / sizing_price[t][s]
delta         = target_shares - shares[s]

direction     = sign(delta)
exec_px       = price[t][s] * (1 + direction * slippage_bps / 1e4)   # 买入更贵、卖出更便宜
gross         = delta * price[t][s]                                   # 按理想价计价的成交额
commission    = |delta| * exec_px * commission_bps / 1e4
slippage_cost = |delta| * price[t][s] * slippage_bps / 1e4

cash  -= delta * exec_px + commission
shares[s] = target_shares
```

要点：

- **成本对买卖双方都是支出**：滑点通过 `exec_px` 进入现金，佣金单独扣减。
- `sizing_price = exec_price` 为默认口径：下单时用当日已知的开盘价确定股数，既无前视，也不必预估当日收盘价。
- 现金不足时（`clip_to_cash=True`）按可用现金等比缩量买入，并在 `BacktestResult.warnings` 记录 `("cash_clipped", date, symbol)`；不允许现金为负。
- `integer_shares=True` 时向下取整（A 股可再加 `lot_size=100`），取整残差留在现金中。

### D5. 双账本交叉校验

提供两种会计模式，`accounting="shares"` 为默认：

| 模式 | 做法 | 用途 |
| --- | --- | --- |
| `shares` | 逐 bar 维护 cash + 股数，按收盘估值 | 权威结果，输出持仓与交易流水 |
| `weights` | 向量化：`equity[t] = equity[t-1] * (1 + Σ w_exec[t] * ret[t]) - cost[t]` | 快速筛查与交叉校验 |

**关键测试**：零成本、`allow_short=False`、`target_exposure<=1`、无现金裁剪时，两种模式的净值序列应逐点一致（容差 1e-9）。这条测试能同时抓住「股数/现金账本写错」和「权重法漏算换手」两类 bug。

### D6. 结果结构

```python
@dataclass
class BacktestResult:
    equity: pd.Series  # 每日净值
    returns: pd.Series  # 每日收益率
    weights: pd.DataFrame  # 每日收盘实际权重（含价格漂移，非目标权重）
    positions: pd.DataFrame  # 每日持股数
    trades: pd.DataFrame  # date, symbol, shares, price, gross, commission, slippage, cost, reason
    turnover: pd.Series  # 每日换手率（单边成交额 / 净值）
    costs: pd.DataFrame  # 每日 commission / slippage / total
    config: BacktestConfig
    meta: DataMeta | None
    warnings: list[tuple[str, str, str]]

    def summary(self) -> pd.Series: ...  # 委托 metrics.summarize
```

### D7. 入口

```python
def run_backtest(prices: pd.DataFrame, target_weights: pd.DataFrame,
                 config: BacktestConfig | None = None,
                 meta: DataMeta | None = None) -> BacktestResult

def run_signal_backtest(prices: pd.DataFrame, signal: pd.DataFrame, *,
                        target_exposure: float = 1.0,
                        config: BacktestConfig | None = None) -> BacktestResult
```

`run_signal_backtest` 内部固定走「`signal_to_weights` → `run_backtest`」，把批次 C 与 D 串成一条不可绕过滞后处理的路径。

### D8. 测试清单（逐条对应上方设计）

| # | 测试 | 断言 |
| --- | --- | --- |
| 1 | 手算一致性 | 5 日样例、零成本、满仓，最终净值与手算逐笔相等（1e-9） |
| 2 | 成本单调性 | `commission_bps` 递增 → 终值单调不增；零成本终值 ≥ 有成本终值 |
| 3 | 无前视（因果性） | 把 t≥k 的价格全部改写，t<k 的 `equity` 与 `trades` 完全不变 |
| 4 | 首 bar 无交易 | `lag=1` 时第一个 bar 的 `trades` 为空 |
| 5 | 双账本一致 | `shares` 与 `weights` 模式在零成本下净值逐点一致 |
| 6 | 多空开关 | `allow_short=False` 时 `weights` 最小值 ≥ -1e-12 |
| 7 | 现金不为负 | 任意参数下 `cash` 序列最小值 ≥ 0（`clip_to_cash=True`） |
| 8 | 换手可控 | `turnover` 均值与「信号翻转频率 × 2 × 敞口」量级一致 |
| 9 | 缺失数据处理 | 价格含 NaN 的标的当日不产生交易，且被记录到 `warnings` |
| 10 | 空信号 | 全 0 信号 → 净值恒等于 `initial_cash`，`trades` 为空 |

### D9. DoD

- `run_signal_backtest(样例面板, ma_cross信号)` 输出完整 `BacktestResult`，上表 10 条测试全绿。
- 文档 [CONVENTIONS.md](CONVENTIONS.md) 第二节「成交价：未定义」的状态更新为「默认 `open`（次日开盘）」，并注明该假设对结果的影响。

---

## 八、批次 E：最小评估门槛

### E1. 指标扩展（`metrics.py`）

新增纯函数，全部接受 `pd.Series` 收益：

- 收益：`cumulative_return`、`cagr`、`annualized_volatility`
- 风险：`sortino_ratio`、`calmar_ratio`、`drawdown_table`（回撤区间表：起止日期、深度、恢复天数、水下时长）
- 交易层面：`turnover`、`exposure`（平均持仓敞口）；`hit_rate`、`profit_factor` 由 `trades` 计算（放 `backtest` 侧或 metrics 侧接受 trades）
- 汇总：`summarize(returns, benchmark=None, periods_per_year=252) -> pd.Series`，一次输出一张指标表
- 基准对照：`relative_return`、`alpha_beta`、`tracking_error`、`information_ratio`

### E2. 随机对照（最小版）

`benchmark_buy_hold(prices)` 返回买入持有收益；`random_signal(prices, seed)` 返回随机 0/1 信号。**近期不做**统计显著性检验（Deflated Sharpe 等留到 P6），只做「策略 vs 基准 vs 随机」三栏并列输出，让结论无法只看绝对收益。

### E3. 补测试

- `annualized_sharpe`：常量序列返回 0、空序列、单元素序列、`periods_per_year<=0` 抛错。
- `max_drawdown`：空序列返回 0、单调上涨为 0、含 -100% 抛错、已知样例的数值断言。
- `summarize`：含基准时指标表含 `alpha`/`beta` 列。

### E4. DoD

一次运行输出一张指标表，可直接回答「这个策略是否跑赢买入持有、回撤多大、换手是否吃掉收益」。

---

## 九、端到端调用形态（目标态）

```python
from quant_research.backtest import BacktestConfig, ExecutionConfig, run_signal_backtest
from quant_research.config import load_settings
from quant_research.data import load_price_panel
from quant_research.metrics import benchmark_buy_hold, summarize
from quant_research.strategies import get_signal

settings = load_settings()
panel = load_price_panel("data/sample/sample_prices.csv")  # 含 DataMeta

signal = get_signal("ma_cross")(panel.prices, fast_window=20, slow_window=50)

result = run_signal_backtest(
    panel.prices,
    signal,
    target_exposure=1.0,
    config=BacktestConfig(
        initial_cash=settings.backtest.initial_cash,
        costs=settings.backtest,  # commission_bps / slippage_bps
        execution=ExecutionConfig(price="open"),
        allow_short=settings.backtest.allow_short,
    ),
    meta=panel.meta,
)

summary = summarize(result.returns, benchmark=benchmark_buy_hold(panel.prices))
print(summary)
print(result.trades.head())
```

---

## 十、关键取舍与理由

| 取舍 | 选择 | 理由 |
| --- | --- | --- |
| 回测实现方式 | 自建引擎，逐 bar 循环 | 成本模型与成交假设必须完全透明可审计（ARCHITECTURE 技术选型已定） |
| 会计模式 | 默认 `shares`，保留 `weights` 交叉校验 | 需要交易流水与现金；双账本互验能抓住账本 bug |
| 默认成交价 | `open`（次日开盘） | 收盘价成交隐含「已知收盘价下单」，是最常见的前视来源 |
| 股数口径 | 用执行价定股数 | 无前视，且避免现金不足；`prev_close` 作为备选口径保留 |
| 配置返回类型 | 冻结 dataclass | 拼错字段静默生效会让「无成本回测」这类错误无声通过 |
| 缓存 | 首版不建 | 当前数据量下，缓存的复杂度大于收益 |
| 类型检查 | 只对新增目录强制 | 一次性给存量加类型会稀释本阶段重点 |

---

## 十一、明确不做（近期）

按实施路径第 6~8 步与「优先原则」，以下工作在本计划内**不启动**：

- 多标的因子库、截面标准化与中性化、行业暴露约束
- 组合优化器（风险平价、波动率倒数、约束求解）
- 实验配置驱动运行、参数扫描、walk-forward、Deflated Sharpe
- 结果归档到 `reports/<exp_id>/`、HTML/Markdown 报告、matplotlib 图表
- 纸面验证、定时数据更新、任何券商或实盘连接
- Parquet 缓存、真实数据源接入、多资产类别

---

## 十二、与 ROADMAP 的映射

| 本计划批次 | ROADMAP 阶段 |
| --- | --- |
| A | P0 工程地基 |
| B | P1 数据层（配置加载器、样例数据、质量报告；不含缓存与复权处理） |
| C | P2 信号层（契约、注册表、滞后；不含因子库与截面预处理） |
| D | P3 组合构建（仅固定仓位）+ P4 回测引擎 |
| E | P5 绩效分析（核心指标与基准；不含 IC/IR、滚动指标） |

顺序与 ROADMAP 的「P0 → P1 → P4 → P5 → P2/P3 → P6 → P7」一致：先纵向打通最小闭环，再横向扩充。

## 十三、落地记录（v0.2.0）

批次 A~E 已实现：`pytest` 69 条通过，`ruff check` 与 `mypy` 无告警。以下记录实现期相对本设计的调整，避免文档与代码再次分叉。

### 接口补充（设计时遗漏，实现时补上）

| 补充 | 原因 |
| --- | --- |
| `BacktestResult.cash` | 账本要能被审计，只有净值不足以定位问题 |
| `run_backtest(..., exec_prices=...)` | `execution.price="open"/"vwap"` 需要外部价格面板；缺省会明确报错而不是退化为收盘价 |
| `BacktestConfig.from_settings(settings, **overrides)` | 把 `settings.yaml` 的成本参数一次性贯通，同时允许按实验覆盖成交假设 |
| `PricePanel.field()` / `has_field()` / `extra` | `close` 之外的 OHLCV 需要随面板一起传递，才能支撑开盘价成交 |
| `strategies/random.py`（注册名 `random`） | 随机对照信号属于策略层，放进 `metrics` 会破坏依赖方向 |

### 行为调整

1. **同一 bar 内先卖后买**：卖出释放的现金供买入使用，否则全仓账户的再平衡会被误判为现金不足。
2. **买入预算预留成本**：目标股数按「含滑点与佣金的有效单价」折算，避免每天因最后一分钱被裁剪。
3. **裁剪告警降噪**：只有裁剪金额超过权益 0.5% 才记 `cash_clipped`——全仓账户无法为漂移修正付款是真实约束，不是异常。
4. **默认成交价为前一 bar 收盘**：`price="close"` 时执行价取 `close.shift(lag)`，这样 bar t 的收益归属于持仓，`shares` 与 `weights` 两种账本在零成本下才能逐点一致。`open` / `vwap` 成交需显式提供面板。
5. **`win_rate` / `profit_factor` 按周期收益统计**：按平仓笔数统计需要交易配对，留待后续；当前口径已在 docstring 中写明。
6. **`describe_panel` 只报告不修复**：与「数据修复必须是显式决策」的约定一致。

### 计划勘误

样例数据放在 `data/sample/`，该目录本来就不在 `.gitignore` 的忽略范围内（只忽略 `data/raw/`、`data/processed/`、`data/cache/`），因此 B3 提到的白名单规则并不需要。

### 遗留（下一批次）

- **换手缓冲带与最小交易阈值**：当前每日再平衡到目标权重会让价格漂移产生小额交易（样例数据日均换手约 7%），需要 `rebalance_threshold` 或最小交易额。
- 组合约束（单标的上限、杠杆、净敞口）、因子评价（IC / IR / 分层）、滚动指标、实验归档与报告、Parquet 缓存。
- 首次 Git 提交尚未完成（需要人工确认后执行）。

## 相关文档

- [IMPLEMENTATION_BRIEF.md](IMPLEMENTATION_BRIEF.md) — 最终态、阶段门槛、实施路径与投入估算
- [ROADMAP.md](ROADMAP.md) — 分期任务清单与 DoD
- [ARCHITECTURE.md](ARCHITECTURE.md) — 分层、依赖方向与数据形态约定
- [API.md](API.md) — 公开函数参考（新增接口需同步登记）
- [DATA.md](DATA.md) — 数据格式规范与元信息要求
- [CONVENTIONS.md](CONVENTIONS.md) — 研究约定与回测防坑清单
