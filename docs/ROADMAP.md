# 路线图 Roadmap

本文档按阶段拆解 `quant-research` 的建设路径。每个阶段包含**目标、任务清单、完成标准（DoD）**。

最后更新：v0.1.0

---

## 目标与边界

**目标**：建成一套**可复现、可评估、防过拟合**的量化研究框架——输入是数据与策略想法，输出是可信的绩效结论。

**边界（明确不做）**：

- 实盘下单、券商 API、自动交易
- 实时行情接入、Tick 级 / 高频策略
- 任何形式的托管或生产环境部署

项目长期保持「离线研究 + 纸面验证」的定位，与 README 中的声明一致。

---

## 现状盘点

已完成的基础（v0.2.0）：

- ✅ src-layout 打包结构（`pyproject.toml` + setuptools）
- ✅ `pytest`、`ruff`、`mypy`（渐进式）、CI 与 pre-commit
- ✅ 数据 → 信号 → 仓位 → 含成本回测 → 指标表的最小闭环
- ✅ `shares` / `weights` 双账本交叉校验，零成本下逐点一致
- ✅ 69 条测试，覆盖校验、契约、账本、成本与时序
- ✅ 中文 README 与研究约定

已知缺口（按严重度排序）：

1. **横截面能力几乎空白**——无因子库、去极值/中性化预处理，也无 IC / IR 与分层回测。
2. **无实验归档**——参数、结果、数据指纹与代码版本仍靠手工记录，`reports/` 为空。
3. **样本外与稳健性流程未建立**——无 walk-forward、参数敏感性分析与滚动指标。
4. **数据工程仍有缺口**——派生缓存、跨标的日历对齐、上市首日与停牌处理尚未完成
   （原始数据的每日采集与 Parquet 落盘已具备）。
5. **组合约束不完整**——无行业 / 板块暴露上限与容量约束。
6. **工作台只剩认证缺口**——任务、采集、实验台账、参数网格都已有历史与对照，
   无认证（详见 [UI_DESIGN.md](UI_DESIGN.md) 的取舍与演进路径）。

---

## P0 — 工程地基

**目标**：让项目可协作、可回溯，使任何一次提交都可被信任。

- [ ] 创建首次提交（仓库已 `git init`，`master` 分支**尚无提交**，全部文件未跟踪）
- [x] 补齐 `.gitignore` 中已引用但缺失的占位文件：`data/raw/.gitkeep`、`data/processed/.gitkeep`、`data/cache/.gitkeep`、`reports/.gitkeep`
- [x] 添加 CI（GitHub Actions）：Python 3.11 / 3.12 矩阵下运行 `ruff check` + `pytest`
- [x] 添加 `pre-commit`：ruff format + ruff check
- [x] 添加 `LICENSE` 与 `CHANGELOG.md`
- [x] 引入静态类型检查：`mypy` 已配置，先只对 `backtest/`、`portfolio/` 强制
- [x] 统一日志：`logging_setup.get_logger`，模块内不再使用 `print`

**DoD**：新克隆的仓库执行 `pip install -e '.[dev]' && pytest` 全绿；每次 PR 自动跑 lint + 测试。

---

## P1 — 数据层

**目标**：从「读单个 CSV」升级为「可复现、可校验的多标的面板数据管线」。

- [x] 配置加载器：`config/settings.yaml` → 冻结 dataclass，未知键报错
- [x] 泛化数据加载：`load_price_panel` 支持 OHLCV 多列、目录 glob 批量拼接
- [x] 面板化：输出 `DataFrame(index=date, columns=symbol)`，支撑横截面研究
- [x] 每日采集进程：`quant-collect` 增量落盘 Parquet（akshare / mock 两个 provider），
      单次与常驻两种模式，见 [DATA_COLLECTION.md](DATA_COLLECTION.md)
- [x] Parquet 落盘与读取：`data/raw/<provider>/daily/<SYMBOL>.parquet`，目录布局自动识别
- [ ] 派生缓存至 `data/cache/`，以「源文件 + mtime + 参数」为 key，可安全重建
- [x] 复权处理：`DataMeta.price_adjustment` 显式记录复权口径
- [ ] 缺失值与停牌：前向填充上限、上市首日跳过、交易日历对齐（当前仅有 `missing_policy="keep"`）
- [x] 数据质量报告：`describe_panel` 输出区间、缺失率、异常跳变检测
- [x] 补测试：`load_close_prices` 的四类校验（缺列、重复日期、缺失值、非正价格）
- [x] 内置样例数据：`data/sample/sample_prices.csv`，README 示例离线可跑

**DoD**：一条命令即可把 `data/raw/` 下的多个 CSV 转成带元信息的对齐面板，并产出质量报告。

---

## P2 — 信号 / 因子层

**目标**：建立统一的信号契约，让新策略可插拔、可批量评估。

- [x] 定义信号契约：`Callable[[pd.DataFrame], pd.DataFrame]`（日期 × 标的 → 分数或目标权重）
- [x] 策略注册表：`STRATEGY_REGISTRY` + `get_signal()`，内置 `ma_cross` / `random`（未按族分组）
- [x] 升级 `moving_average_signal`：支持 DataFrame 多标的、`min_periods` 可配，补测试
- [ ] 因子库：横截面动量、短周期反转、波动率、成交量
- [ ] 截面预处理：去极值（winsorize）、标准化（z-score）、市值/行业中性化
- [x] 执行滞后工具：`apply_lag()`，且引擎内部强制滞后一次
- [x] 单测要求：每个信号至少 1 条正常路径 + 1 条异常路径

**DoD**：新增一个策略只需新增一个文件并注册，主流程零改动。

---

## P3 — 组合构建

**目标**：把「信号分数」翻译成「可交易的目标仓位」。

- [x] 权重分配器：固定仓位、等权、波动率倒数（风险平价 / softmax 未做）
- [x] 约束（基础版）：单标的上限、总杠杆、多空净敞口（`PortfolioPolicy`；行业 / 板块暴露上限未做）
- [x] 换手控制：目标仓位缓冲带（buffer）、最小交易额定阈值（`RebalancePolicy`）
- [x] 多空支持：`allow_short` 由配置贯通到权重、策略与引擎（负分数不自动开仓）
- [x] 单元测试：权重形状、敞口缩放、负分数处理、约束幂等性、换手下降幅度

**DoD**：给定信号分数与约束，输出满足全部约束的目标权重，且换手率可被显式控制。

---

## P4 — 回测引擎（最高优先级）

**目标**：补齐当前最大的空白——从目标仓位到净值曲线的撮合执行层。

- [x] 引擎核心：逐 bar 事件循环（`shares` 账本）+ 向量化近似（`weights` 模式）
- [x] 成本模型：消费 `settings.yaml` 的 `commission_bps` 与 `slippage_bps`
- [x] 成交假设：收盘价成交 / 次日开盘成交 / VWAP，可配置；默认收盘、`lag=1`
- [x] 账本：现金、持仓、市值、净值曲线（equity curve）
- [x] 交易流水：`trades` DataFrame（时间、标的、数量、成交价、成本、原因）
- [x] 前视偏差防线：引擎内部强制 `shift`，并有因果性测试断言
- [x] 单元测试：零成本时结果与手工计算一致；有成本时净值单调下降

**DoD**：跑通「数据 → 信号 → 仓位 → 回测 → 净值曲线」全链路，且结果与手算一致。

---

## P5 — 绩效与风险分析

**目标**：把现有 2 个指标扩展成足以支撑决策的分析集。

- [x] 收益维度：CAGR、累计收益、年化波动
- [x] 风险调整：Sortino、Calmar（Omega 未做）
- [x] 回撤分析：回撤区间表 `drawdown_table`（起止、谷底、深度、恢复天数、是否恢复）
- [ ] 交易层面：胜率、盈亏比按**周期收益**已提供；平均持仓期、按平仓笔数统计未做
- [ ] 因子评价：IC、IR、分层回测（quantile portfolio）
- [x] 基准对比：相对收益、Alpha / Beta、跟踪误差、信息比率
- [ ] 滚动指标：滚动夏普、滚动最大回撤
- [x] 补测试：`annualized_sharpe`、`max_drawdown` 及新增指标

**DoD**：一次运行输出一张指标表，能直接回答「这个策略值不值得继续投入」。

---

## P6 — 实验管理与可复现

**目标**：让每一次实验都能被复现、被比较、被证伪。

- [ ] 配置驱动运行：`experiments/<name>.yaml` → 一次完整运行
- [ ] 结果落盘：`reports/<exp_id>/` 下保存 `params.yaml`、`metrics.json`、`equity.csv`、`trades.csv`、`git_sha.txt`
- [ ] 参数扫描：网格 / 随机搜索 + 结果汇总表
- [ ] 样本外验证：walk-forward、滚动训练 / 测试窗口切分
- [ ] 过拟合防护：多重检验校正（Deflated Sharpe）、参数敏感性热力图
- [ ] 随机对照：随机信号、买入持作为显著性参照基准

**DoD**：任意一个历史实验都能凭 `exp_id` 一键复现出相同结果。

---

## P7 — 可视化与报告

**目标**：让结论可被快速阅读和分享。

- [ ] 引入 `matplotlib`（当前未依赖）
- [ ] 标准图表：净值曲线 + 回撤、月度收益热力图、滚动夏普、持仓权重
- [ ] 自动生成 HTML / Markdown 报告到 `reports/`
- [ ] 提供 notebook 研究模板（`notebooks/` 目前为空）

**DoD**：`reports/` 下产出一份可直接分享的实验报告。

---

## 里程碑规划

| 版本 | 包含阶段 | 标志 |
| --- | --- | --- |
| v0.2 | P0 + P1 | 数据管线可用，项目纳入版本控制 |
| v0.3 | P2 + P3 | 多策略可插拔，目标仓位可构建 |
| v0.4 | P4 | **完整回测链路跑通** |
| v0.5 | P5 | 绩效分析体系完整 |
| v0.6 | P6 | 实验可复现、可比较 |
| v0.7 | P7 | 报告自动化 |
| v1.0 | 全阶段 | 稳定、可信的量化研究框架 |

## 建议执行顺序

**P0 → P1 → P4 → P5 → P2/P3 → P6 → P7**

优先打通「数据 → 回测 → 绩效」这条最小闭环，再横向扩充策略库。
这样每新增一个策略都能立刻得到量化评估，避免在信号层堆积却没有衡量标准。

## 相关文档

- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — 近期批次任务与接口级细化设计
- [ARCHITECTURE.md](ARCHITECTURE.md) — 数据流与分层设计
- [API.md](API.md) — 公开函数参考
- [DATA.md](DATA.md) — 数据格式规范
- [CONVENTIONS.md](CONVENTIONS.md) — 研究约定与回测防坑清单
