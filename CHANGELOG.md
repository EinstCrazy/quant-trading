# 更新日志 Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的格式，版本号遵循语义化版本。

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
