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
| `quant_research.portfolio` | ✅ 可用 | 固定/等权/波动率倒数三种权重；`PortfolioPolicy` 约束（单标的、杠杆、净敞口、多空） |
| `quant_research.backtest` | ✅ 最小版 | 日频撮合、手续费、滑点、现金/持仓账本、交易流水、双账本互验、换手缓冲带与最小成交额 |
| `quant_research.metrics` | ✅ 核心集 | CAGR / 夏普 / Sortino / Calmar / 回撤表 / 基准对照；IC·IR 未实现 |
| 实验管理、可视化、报告 | ❌ 未实现 | `notebooks/` 与 `reports/` 仍为空 |
| 版本控制 | ✅ 就绪 | 首次提交已完成，CI 与 pre-commit 生效 |

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

组合约束与换手控制按需叠加（两者都会压低换手，也会改变收益）：

```python
from quant_research.backtest import PortfolioPolicy, RebalancePolicy

result = run_signal_backtest(
    panel.prices,
    signal,
    weight_method="equal_weight",
    policy=PortfolioPolicy(max_weight=0.25, max_gross=1.0),
    exec_prices=panel.field("open"),
    config=BacktestConfig.from_settings(
        settings,
        execution=ExecutionConfig(price="open"),
        rebalance=RebalancePolicy(buffer=0.05, min_trade_notional=1_000.0),
    ),
)
```

`data/sample/sample_prices.csv` 是固定随机种子生成的**合成数据**，由
`python scripts/generate_sample_data.py` 重建，详见 [data/sample/README.md](data/sample/README.md)。

真实 A 股数据由采集进程落到 `data/raw/`（不入库）：

```bash
pip install -e '.[collect]'
quant-collect --symbols 600519.SH,000001.SZ --start 2024-01-01   # 单次收集
quant-collect --daemon --at 18:30                                # 每个交易日收盘后
```

随后 `load_price_panel("data/raw/akshare/daily")` 即可读到面板。安装、调度与故障处理见
[docs/DATA_COLLECTION.md](docs/DATA_COLLECTION.md)。

数据够了就可以打分选股（研究用的候选名单，不是投资建议，也不接实盘）：

```bash
quant-screen --provider baostock --top-n 10   # 收盘后打分，出名单并归档
quant-screen --provider baostock --review     # 回头看这些名单到底行不行
```

指标因子默认关闭（它们与已有因子高度相关，开之前先看
[docs/INDICATORS.md](docs/INDICATORS.md) 的相关性表）：

```bash
quant-screen --indicator-factors macd_trend --factor-weights momentum=1,macd_trend=0.5
```

因子到底有没有用，用 IC 与分层量一遍（结论与边界见
[docs/FACTORS.md](docs/FACTORS.md)）：

```bash
quant-factor --provider baostock --horizon 5                    # 全部因子一张表
quant-factor --provider baostock --factors momentum --detail momentum   # 分层 + 衰减
```

价格之外还有一层否决权：财报里的红旗（亏损、高杠杆、利润不含现金等），
说明与边界见 [docs/FUNDAMENTALS.md](docs/FUNDAMENTALS.md)：

```bash
quant-fundamentals --provider baostock              # 财报红旗 + 立案调查提醒
quant-screen --provider baostock --exclude-risky    # 剔除高风险后再选股
```

被证监会立案调查的标的属于**硬否决**，选股默认排除。数据来自巨潮资讯的标题检索
（证监会官网没有「立案调查」栏目），回看一年约 2 秒，细节见
[docs/REGULATORY.md](docs/REGULATORY.md)：

```bash
quant-screen --provider baostock --allow-investigated   # 放行，查看完整名单
```

每晚一条命令（等到数据落地 → 收数 → 打分 → 归档，数据没到就不打分）：

```bash
quant-nightly --provider baostock             # 等到 collection.schedule_at 再跑
quant-nightly --provider baostock --daemon    # 每个交易日自动跑一次
```

技术指标（参数在 `config/settings.yaml` 的 `indicators` 段，公式与坑见
[docs/INDICATORS.md](docs/INDICATORS.md)）：

```bash
quant-indicators --provider baostock --symbols 600519.SH   # 拉数并计算
quant-indicators --from-cache                              # 用已缓存的面板，不联网
```

本地工作台（可选）：

```bash
pip install -e '.[web]'
quant-ui                 # 打开 http://127.0.0.1:8000
```

六个视图：数据质量、回测、策略对比、采集状态与补数、今日建议、技术指标。设计与取舍见
[docs/UI_DESIGN.md](docs/UI_DESIGN.md)。

代码输入框会边打边补全，候选里同时给出中文名（先刷一次名称表，之后不用再管）：

```bash
quant-symbols --refresh              # 拉全市场代码+名称（baostock 约 8 秒）
quant-symbols -q 茅台                # 命令行也能查：600519.SH 贵州茅台
```

输入 `600519`、`sh600519` 或 `茅台` 都能命中，细节见 [docs/SYMBOLS.md](docs/SYMBOLS.md)。

## 项目结构

```text
quant-trading/
├── .github/workflows/      CI（ruff + pytest，Python 3.11 / 3.12）
├── .pre-commit-config.yaml ruff 格式化与检查
├── config/                 研究默认配置（数据目录、回测成本假设）
├── data/
│   ├── sample/             合成样例数据（入库，可重建）
│   ├── raw/                采集落盘的原始数据（不入库）
│   ├── processed/          清洗、对齐后的数据（不入库）
│   └── cache/              派生缓存（不入库）
├── notebooks/              交互式研究
├── reports/                回测图表、采集报告与实验结论（不入库）
├── scripts/                取数、样例生成等辅助脚本
├── src/quant_research/
│   ├── config.py           YAML → 冻结 dataclass
│   ├── collector.py        每日采集流程与调度循环
│   ├── nightly.py          尾盘编排：收数 → 校验落地 → 打分 → 归档
│   ├── cli.py              `quant-collect` 命令行入口
│   ├── screen_cli.py       `quant-screen` 命令行入口
│   ├── nightly_cli.py      `quant-nightly` 命令行入口
│   ├── indicators.py       技术指标：宽表进宽表出，附指标说明目录
│   ├── indicators_cli.py   `quant-indicators` 命令行入口
│   ├── web/                `quant-ui` 本地工作台（FastAPI + 静态前端）
│   ├── data/               面板加载、元信息、质量报告、providers/、store、面板缓存
│   ├── strategies/         信号契约、注册表、动量与随机信号
│   ├── scoring/            截面因子、逐日标准化、选股与选股台账
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
| [docs/DATA_COLLECTION.md](docs/DATA_COLLECTION.md) | 每日 A 股采集：安装、落盘结构、调度与故障处理 |
| [docs/UI_DESIGN.md](docs/UI_DESIGN.md) | 本地工作台：架构、视图、API 契约与演进路径 |
| [docs/INDICATORS.md](docs/INDICATORS.md) | 常用技术指标：公式、参数与实现细节（RSI / MACD / BOLL / KDJ / ATR 等） |
| [docs/FACTORS.md](docs/FACTORS.md) | 因子检验：IC、分层收益、衰减，以及为什么不能拿样本内结果调参数 |
| [docs/FUNDAMENTALS.md](docs/FUNDAMENTALS.md) | 财报红旗：六个阈值、脏数据校验、金融口径豁免与防前视 |
| [docs/REGULATORY.md](docs/REGULATORY.md) | 证监会立案调查：状态推导、关键词去噪、增量扫描与硬否决 |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | 研究约定与回测防坑清单 |

## 路线图摘要

主线是先把「数据 → 回测 → 绩效」的最小闭环打通，再横向扩充策略库：

| 阶段 | 主题 | 关键产出 |
| --- | --- | --- |
| P0 | 工程地基 | Git 初始化、CI、pre-commit |
| P1 | 数据层 | 多标的面板、Parquet 落盘、每日采集进程、配置加载器 |
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
