# `quant_research` 源码导览

本目录是可安装的 Python 包，放置可复用的研究逻辑。探索过程、临时分析和实验产物分别放在仓库的 `notebooks/` 与 `reports/`，不要混入包代码。

## 模块结构

| 路径 | 职责 | 主要入口 |
| --- | --- | --- |
| `config.py` | YAML 研究配置 → 冻结 dataclass | `load_settings` |
| `data/` | 面板加载、元信息与质量报告 | `load_price_panel`、`describe_panel` |
| `strategies/` | 信号契约、注册表与信号实现 | `get_signal`、`apply_lag`、`moving_average_signal` |
| `portfolio/` | 信号分数 → 目标权重 | `signal_to_weights` |
| `backtest/` | 撮合、成本、账本与日频引擎 | `run_backtest`、`run_signal_backtest` |
| `metrics.py` | 绩效、风险与基准指标 | `summarize`、`annualized_sharpe` |
| `logging_setup.py` | 统一日志入口（不使用 `print`） | `get_logger` |

包的公开接口以各模块的 `__all__`（如有）和函数签名为准。当前详细参数、返回值、异常与示例统一维护在 [API 参考](../../docs/API.md)。

## 文档分工

- 本文件负责源码目录导航与模块职责，不重复完整 API 说明。
- Python 模块和公开函数的 docstring 说明本模块行为、参数语义及重要边界条件。
- 仓库级设计与研究约定分别维护在 [架构设计](../../docs/ARCHITECTURE.md)、[数据规范](../../docs/DATA.md) 和 [研究约定](../../docs/CONVENTIONS.md)。
- 新增模块时，先在上表登记职责和主要入口；详细 API 变化同步更新 `docs/API.md`。

## 依赖方向

模块保持职责清晰：策略消费数据并生成信号，指标模块提供可复用的计算函数；通用模块不要依赖更高层的研究流程。新增跨模块依赖前，先确认不会形成循环导入。
