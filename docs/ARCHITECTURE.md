# 架构设计 Architecture

## 数据流

量化研究的主链路可以拆成五层。v0.2.0 已把五层串成最小闭环：数据、信号、组合（固定/等权）、
日频回测、绩效分析。空缺集中在横截面因子、组合约束、实验归档与报告。

```text
⓪ 采集        行情源  →  quant-collect（provider → store）  →  data/raw/<provider>/daily/*.parquet
                                                    │
① 数据层      CSV/Parquet  →  清洗校验  →  面板数据 DataFrame(date × symbol)
                                                    │
② 信号层      面板数据      →  因子/信号  →  分数 DataFrame(date × symbol)
                                                    │  ⚠️ 需 shift(1) 滞后
③ 组合层      分数          →  权重分配 + 约束  →  目标权重
                                                    │
④ 回测层      目标权重 × 行情 →  撮合 + 成本  →  净值曲线 / 交易流水
                                                    │
⑤ 分析层      净值曲线      →  绩效指标 / 归因  →  实验结论
```

当前代码对应的实际链路：

```python
panel = load_price_panel("data/sample/sample_prices.csv")  # ① 含 DataMeta
signal = get_signal("ma_cross")(panel.prices, 20, 50)  # ② 未滞后
result = run_signal_backtest(  # ③④ 权重转换 + 引擎内部 shift(lag)
    panel.prices,
    signal,
    exec_prices=panel.field("open"),
)
summary = result.summary(benchmark_buy_hold(panel.prices))  # ⑤
```

## 分层职责

| 层 | 目录 | 职责 | 状态 |
| --- | --- | --- | --- |
| 采集 | `quant_research/collector.py`、`data/providers/`、`data/store.py` | 每日增量拉取、落盘、记账 | 可用（日频，akshare / baostock / sse / mock） |
| 数据 | `quant_research/data/` | 加载、校验、元信息、质量报告 | 可用（派生缓存与复权修复未做） |
| 信号 | `quant_research/strategies/` | 信号契约、注册表、因子计算 | 可用（因子库未做） |
| 组合 | `quant_research/portfolio/` | 信号 → 目标权重、约束 | 可用（三种权重 + 单标的/杠杆/净敞口约束） |
| 回测 | `quant_research/backtest/` | 撮合、成本、账本、交易流水、换手控制 | 可用（日频） |
| 分析 | `quant_research/metrics.py` | 绩效、风险与基准指标 | 核心集（无 IC/IR、滚动指标） |
| 实验 | 待建 `quant_research/experiments/` | 配置驱动运行、结果落盘 | 未实现 |

## 核心设计约定

### 1. 单向依赖

依赖只能从上往下，禁止反向引用：

```text
experiments → backtest → portfolio → strategies → data
```

下层模块**不得** import 上层模块。`metrics` 作为纯函数工具集，可被任意层引用。

> 现状：三个模块彼此零依赖，尚未形成链路——这是后续建设的起点而非问题。

### 2. 数据形态

统一使用 **日期为索引、标的为列** 的 `pd.DataFrame`。单标的场景用 `pd.Series` 亦可，
但跨层接口一律以 DataFrame 为准，避免单标的写好后无法扩展。

| 对象 | 形态 | 说明 |
| --- | --- | --- |
| 价格面板 | `DataFrame(date × symbol)` | 复权收盘价 |
| 信号 / 分数 | `DataFrame(date × symbol)` | 未经滞后的原始输出 |
| 目标权重 | `DataFrame(date × symbol)` | 各行之和 = 目标总敞口 |
| 收益序列 | `Series(date)` | 组合层面的周期收益 |

### 3. 前视偏差的唯一防线

**信号在 t 时刻产生，只能在 t+1 时刻成交。** 所有信号函数返回**未经滞后**的原始值，
由回测层（`run_backtest` 内部的 `weights.shift(lag)`）统一执行一次。

这样约定是为了：信号函数保持纯粹可测（返回值可直接与手工计算对比），
滞后逻辑集中在唯一位置，便于测试与审计。

### 4. 配置外部化

成本假设、数据路径等一律放在 `config/settings.yaml`，不硬编码在函数默认值里。

现状：`load_settings()` 把配置读成冻结 dataclass（未知键报错），
`BacktestConfig.from_settings()` 把成本与资金假设贯通到引擎；
`result.config.to_dict()` 会回显当次运行的全部假设，便于归档。

## 目录约定

| 目录 | 是否入库 | 说明 |
| --- | --- | --- |
| `src/` | ✅ 入库 | 唯一被安装为包的目录，放可复用逻辑 |
| `tests/` | ✅ 入库 | 自动化测试 |
| `config/` | ✅ 入库 | 不含凭据的默认配置 |
| `notebooks/` | ✅ 入库 | 探索过程，允许临时代码 |
| `data/raw/` | ❌ 不入库 | 原始数据，体积大且可能有授权限制 |
| `data/processed/` | ❌ 不入库 | 可由 raw 重建 |
| `data/cache/` | ❌ 不入库 | 派生缓存，可随时重建 |
| `reports/` | ❌ 不入库 | 实验产物，但建议归档关键结论 |

判断标准：**能否由其他内容重建**。能重建的不入库，重建成本高的考虑归档。

## 技术选型

| 维度 | 当前选择 | 说明 |
| --- | --- | --- |
| 计算 | `pandas` + `numpy` | 向量化优先，先不上重型框架 |
| 配置 | `pyyaml` | `load_settings()` 读入冻结 dataclass |
| 测试 | `pytest` | 已配置 |
| 代码风格 | `ruff` | line-length 100，target py311 |
| Python | ≥ 3.11 | 代码已使用 `str \| Path` 联合类型语法 |
| 可视化 | 无 | 计划在 P7 引入 `matplotlib` |

当前**未**引入 backtrader / vectorbt / zipline 等现成回测框架，
原因是自建引擎能让成本模型与成交假设完全透明、可控、可审计。
若后续发现维护成本过高，可在 P4 阶段重新评估。

## 相关文档

- [ROADMAP.md](ROADMAP.md) — 分期建设路径
- [API.md](API.md) — 公开函数参考
- [DATA.md](DATA.md) — 数据格式规范
- [CONVENTIONS.md](CONVENTIONS.md) — 研究约定与回测防坑清单
