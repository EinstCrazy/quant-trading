# 更新日志 Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式，版本号遵循语义化版本。

## [未发布]

### Added

- 本地 Web 工作台 `quant-ui`（FastAPI + 零构建前端）：数据质量、回测、策略对比、
  采集状态与补数四个视图。可选依赖 `pip install -e '.[web]'`，默认只监听本机。
- 工作台任务队列：回测 / 对比 / 采集可提交为后台任务，带进度、协作式取消，
  结果归档到 `reports/webui/`。`collect_once` 新增 `on_progress` 与 `should_stop`。
- 工作台历史：重启后从 `reports/webui/` 读回任务列表（结果按需加载），
  采集页可浏览 `reports/collection/` 下最近 10 次采集报告。
- 实验台账 `reports/experiments/`：每次回测留档配置与指标，可按指标排序、加备注，
  并可把历史配置载入为对比变体（`GET` / `PATCH` / `DELETE /api/experiments`）。
- 参数网格 `POST /api/jobs/grid`：一次跑完参数 × 权重方法的组合（上限 200 组，提交前校验），
  按指定指标排名，每组都进实验台账；`BacktestRequest.costs` / `rebalance` 现在允许显式留空。
- 每日 A 股采集进程 `quant-collect`：单次与常驻守护两种模式、按标的增量落盘 Parquet、
  失败隔离与重试、交易日历三级降级、运行报告归档。可选依赖 `pip install -e '.[collect]'`。
- `MarketDataProvider` 抽象与 `akshare` / `mock` 两个实现；`LocalMarketStore` 负责原子写、
  去重合并与 `_state.json` 记账。
- `load_price_panel` / `load_close_prices` 支持 Parquet，并能从 `_state.json` 还原真实复权口径。
- `PortfolioPolicy`：单标的上限、总杠杆、净敞口与多空开关，按水填充确定性缩放（非优化器），幂等。
- `signal_to_weights` 新增 `inverse_volatility` 方法与 `risk` / `policy` 参数；新增 `signal_to_weights_with_policy` 返回约束统计。
- `RebalancePolicy`：换手缓冲带（`buffer`）与最小成交额阈值，只在 `accounting="shares"` 生效。
- `run_signal_backtest` 支持 `policy` 与 `risk`。

### Changed

- 样例数据上的等权组合在开启 `buffer=0.05` 后交易笔数由 703 降至 70，成本下降约 9%。

## [0.2.0] - 2026-10-21

### 新增

- 工程底座：CI（Python 3.11 / 3.12 矩阵）、pre-commit（ruff）、LICENSE、CHANGELOG、统一日志入口。
- 配置层：`load_settings()` 返回冻结 dataclass（`Settings` / `DataSettings` / `BacktestSettings`），未知字段直接报错。
- 数据层：`load_price_panel()` 输出带元信息（`DataMeta`）的价格面板，支持单文件 / 目录两种布局；`describe_panel()` 输出数据质量报告；内置合成样例数据 `data/sample/sample_prices.csv`。
- 信号层：`SignalFn` 契约、策略注册表 `STRATEGY_REGISTRY` 与 `get_signal()`、`apply_lag()`；`moving_average_signal` 支持 DataFrame 多标的。
- 组合层：`signal_to_weights()`，支持固定仓位与等权两种方式。
- 回测层：日频回测引擎 `run_backtest()` / `run_signal_backtest()`，含手续费与滑点、现金与持仓账本、交易流水、换手率，以及 `shares` / `weights` 双账本交叉校验。
- 指标层：`summarize()` 汇总指标表，新增 CAGR / Sortino / Calmar / 回撤区间表 / 胜率 / 盈亏比，以及买入持有基准与随机信号对照。

## [0.1.0]

### 新增

- 项目脚手架：src-layout 打包结构、`pytest` 与 `ruff` 配置。
- `load_close_prices()`：CSV 收盘价加载与四项校验。
- `moving_average_signal()`：双均线 long-only 0/1 信号。
- `annualized_sharpe()`、`max_drawdown()`：基础绩效指标。
- `config/settings.yaml`：数据目录与回测成本默认配置。
