# Quant Trading Research

用于整理量化交易研究、数据、策略实验和回测工具的 Python 工作区。

> **定位**：离线研究 + 模拟回测 + 纸面交易验证。默认**不会**连接券商或提交真实订单。
> 本仓库的信号与指标仅用于方法学研究，不构成投资建议。

## 当前状态（v0.2.0）

**最小闭环已打通**：数据 → 信号 → 目标仓位 → 含成本回测 → 净值曲线 → 指标表，
内置合成样例数据，离线即可运行。

| 模块 / 能力 | 状态 | 说明 |
| --- | --- | --- |
| `quant_research.config` | ✅ 可用 | `settings.yaml` 读入冻结 dataclass，未知键报错 |
| `quant_research.data` | ✅ 可用 | 单标的 CSV + 多标的面板加载、元信息、数据质量报告 |
| `quant_research.strategies` | ✅ 可用 | 信号契约、注册表（`ma_cross` / `random`）、`apply_lag` |
| `quant_research.portfolio` | ✅ 最小版 | 固定仓位与等权；约束、换手缓冲未实现 |
| `quant_research.backtest` | ✅ 最小版 | 日频撮合、手续费、滑点、现金/持仓账本、交易流水、双账本互验 |
| `quant_research.metrics` | ✅ 核心集 | CAGR / 夏普 / Sortino / Calmar / 回撤表 / 基准对照；IC·IR 未实现 |
| 实验管理、可视化、报告 | ❌ 未实现 | `notebooks/` 与 `reports/` 仍为空 |
| 版本控制 | ⚠️ 部分 | 仓库已初始化，CI 与 pre-commit 就绪；首次提交待完成 |

## 快速开始

建议 Python 3.11 或更新版本：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

端到端示例（样例数据 → 信号 → 仓位 → 回测 → 指标）：

```python
from quant_research.backtest import BacktestConfig, ExecutionConfig, run_signal_backtest
from quant_research.config import load_settings
from quant_research.data import load_price_panel
from quant_research.metrics import benchmark_buy_hold
from quant_research.strategies import get_signal

settings = load_settings()
panel = load_price_panel("data/sample/sample_prices.csv")

signal = get_signal("ma_cross")(panel.prices, fast_window=20, slow_window=50)

result = run_signal_backtest(
    panel.prices,
    signal,
    weight_method="equal_weight",
    exec_prices=panel.field("open"),  # 次日开盘成交
    config=BacktestConfig.from_settings(settings, execution=ExecutionConfig(price="open")),
    meta=panel.meta,
)

print(result.summary(benchmark=benchmark_buy_hold(panel.prices)))
print(result.trades.head())
```

引擎内部统一执行 `shift(lag)`，**调用方无需也不应自行滞后**。信号 `1` 表示持有多头，`0` 表示空仓。

`data/sample/sample_prices.csv` 是固定随机种子生成的**合成数据**，由
`python scripts/generate_sample_data.py` 重建，详见 [data/sample/README.md](data/sample/README.md)。
真实数据放入 `data/raw/`（不入库）。

## 项目结构

```text
quant-trading/
├── .github/workflows/      CI（ruff + pytest，Python 3.11 / 3.12）
├── .pre-commit-config.yaml ruff 格式化与检查
├── config/                 研究默认配置（数据目录、回测成本假设）
├── data/
│   ├── sample/             合成样例数据（入库，可重建）
│   ├── raw/                原始数据（不入库）
│   ├── processed/          清洗、对齐后的数据（不入库）
│   └── cache/              派生缓存（不入库）
├── notebooks/              交互式研究
├── reports/                回测图表与实验结论（不入库）
├── scripts/                数据生成等辅助脚本
├── src/quant_research/
│   ├── config.py           YAML → 冻结 dataclass
│   ├── data/               面板加载、元信息、质量报告
│   ├── strategies/         信号契约、注册表、动量与随机信号
│   ├── portfolio/          信号 → 目标权重
│   ├── backtest/           撮合、成本、账本、日频回测引擎
│   ├── metrics.py          绩效、风险与基准指标
│   └── logging_setup.py    统一日志入口
├── tests/                  自动化测试
└── docs/                   项目文档
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [src/quant_research/README.md](src/quant_research/README.md) | 源码模块导览与文档分工 |
| [docs/IMPLEMENTATION_BRIEF.md](docs/IMPLEMENTATION_BRIEF.md) | 项目最终态、中间态、投入估算与实施路径 |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | 近期批次任务与接口级细化设计 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 分期路线图、任务清单与优先级建议 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 数据流、分层设计与目录约定 |
| [docs/API.md](docs/API.md) | 公开函数签名、参数、返回值与异常 |
| [docs/DATA.md](docs/DATA.md) | 数据格式规范、复权与缺失值处理 |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | 研究约定与回测防坑清单 |

## 路线图摘要

主线是先把「数据 → 回测 → 绩效」的最小闭环打通，再横向扩充策略库：

| 阶段 | 主题 | 关键产出 |
| --- | --- | --- |
| P0 | 工程地基 | Git 初始化、CI、pre-commit |
| P1 | 数据层 | 多标的面板数据、Parquet 缓存、配置加载器 |
| P2 | 信号层 | 统一信号契约、因子库、执行滞后 |
| P3 | 组合构建 | 权重分配、约束、换手控制 |
| P4 | **回测引擎** | 撮合循环、成本建模、账本与交易流水 |
| P5 | 绩效分析 | CAGR / Sortino / IC·IR、基准对比、滚动指标 |
| P6 | 实验管理 | 配置驱动运行、结果落盘、walk-forward |
| P7 | 可视化报告 | 净值/回撤/月度热力图、HTML 报告 |

建议执行顺序 **P0 → P1 → P4 → P5 → P2/P3 → P6 → P7**。
理由：先补齐评估手段，之后每新增一个策略都能立刻被量化评估，避免在信号层过度投入却没有衡量标准。
详见 [docs/ROADMAP.md](docs/ROADMAP.md)。

## 研究约定

- 原始数据与凭据不提交到版本控制；数据来源、时区、复权方式和缺失值处理应记录在实验说明中。
- 回测需明确手续费、滑点、成交假设，并避免前视偏差和幸存者偏差。
- 每次实验记录参数、数据区间、代码版本和结论；将可复用逻辑放入 `src/`，探索过程放入 `notebooks/`。
- 先在模拟与纸面交易环境中验证。此工作区没有实盘交易连接。
