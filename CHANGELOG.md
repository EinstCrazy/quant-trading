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
- 数据源：新增 `baostock`（免费、无需 token、接口稳定；停牌日剔除，**成交量原始单位为股，
  已换算为手**以与 akshare 一致）与 `sse`（上交所官方证券清单；无免费历史行情，
  `fetch_daily` 明确报错而非返回空集）。
- 采集失败可读性：源直接断开（ConnectionError / 超时等网络层错误）现归类为
  `SourceUnreachable`，报告写出"疑似限流"的原因、给出换源命令，并把重试退避放慢到
  5 倍以免加剧限流；数据类错误维持原样，不会被误报成限流。
- 选股链路（P8 的 S1/S3/S4）：新增 `data/cache.py`（全市场面板单文件缓存，按
  provider + adjust 分键、按天增量更新、重复运行幂等）与 `scoring/`（截面因子、
  逐日 z-score、选股与可执行性过滤）。要点：
  - 逐日标准化而非全样本：否则周一的分数会依赖全年走势
  - 只保留跑赢当日截面均值的标的，一天选不满 `top_n` 就少给几只
  - 剔除一字板（`open == high == low` 且近涨停）——明日开盘买不进的票不是候选
  - `to_target_weights()` 直接对接组合层（`signal_to_weights`）
  - 防前视自检通过：截断历史重算，同一天的名单与分数完全一致
- 富途（futu）数据源：`providers/futu_provider.py`，支持沪深/港/美日线与**实时快照**
  （`fetch_snapshot`），可选依赖 `pip install -e '.[futu]'`，另需本机运行 OpenD 网关
  （登录无需开户）。定位写进文档：实时行情与下单用它，**全市场日线历史仍用 baostock**——
  富途的历史 K 线按「最近 7 天内的不同标的数」计量（未开户 100 只，1000 只需 50 万 HKD
  资产档），覆盖 A 股全市场一轮要数周。另注意 A 股 LV1 只对**境内 IP** 免费
- 技术指标实现：`indicators.py` 算出 MA/EMA/MACD/RSI/BOLL/KDJ/ATR/威廉%R/CCI/DMI-ADX/
  OBV/VWAP/已实现波动率共 26 个字段，`quant-indicators` 落盘到 `data/indicators/`。
  - 参数全在 `config/settings.yaml` 的 `indicators` 段，非法周期直接拒绝启动
  - 字段名带周期（`rsi_21`）、`meta.json` 记录参数——改口径后旧文件不会被当成新口径读回
  - 数据只有收盘价时静默降级：少算 KDJ/ATR/量能类，而不是拒绝计算
  - 除零全部显式处理：单边行情 RSI=100、完全无波动给 NaN、一字板 KDJ 沿用前值、
    零成交量 VWAP 为缺失
  - `INDICATOR_CATALOG` 把「界面怎么解释这个指标」写进代码，`catalog()` 可直接出 JSON，
    界面与文档共用一份，避免口径漂移
  - 34 条测试：公式手算核对、防前视（截断重算逐值一致）、除零与 NaN 边界、落盘往返。
    实测 baostock 30 只 × 683 日：RSI 8.5–97.0、带宽恒正、VWAP 与收盘同量级
- 技术指标梳理：`docs/INDICATORS.md`——RSI / MACD / BOLL / KDJ / 威廉%R / CCI / ATR /
  ADX / OBV / VWAP 的公式、常用参数与 pandas 实现。重点不是「指标是什么」，而是
  「哪里会写错且不会报错」：`ewm` 默认 `adjust=True` 与行情软件的递推 EMA 不同
  （实测 span=3：10.6667 vs 10.5000）；`ewm` 第一根 K 线就出值，不给 `min_periods`
  就用 1 根 K 线算 14 日 RSI；布林带该用总体标准差（`ddof=0`），pandas 默认 `ddof=1`，
  `n=20` 时带宽差 2.60%；RSI 单边行情、KDJ/威廉一字板、ADX 无波动都是除零；
  `VWAP = amount / (volume × 100)`（项目存的是手和元）。另外写清指标与因子的区别：
  指标是时序的，因子是截面的，不能直接拿 RSI 的绝对值排序
- 指标变因子：三个可选的指标因子接进打分链，默认关闭。
  - `rsi_reversal`（`50 − RSI`，反转）、`macd_trend`（`MACD 柱 ÷ 收盘价`，归一化后
    才能跨价位比较）、`boll_position`（`(收盘−下轨)/(上轨−下轨)`，本就是比例）
  - `screen(indicator_factors=...)` + CLI `--indicator-factors` / `--factor-weights`，
    归档时把因子配置一并记下——「用了哪些因子」无法从名单反推
  - 因子列不再硬编码三列，改成按实际参与的因子生成：新因子自动出现在名单里，
    一份不能分解成分数的名单没法被反驳
  - 拼错因子名直接报错，不静默忽略——静默会改变分数却不改变请求
  - 新增 `factor_correlation()`：逐日算截面 Spearman 再平均。启用指标因子时自动检查，
    超过 0.85 打警告。实测 `boll_position` 与 `ma_distance` 是 0.90、`rsi_reversal`
    与它是 −0.89——布林中轨就是 20 日均线，三个「新视角」里两个是同一个判断换包装。
    阈值先写 0.9，结果 0.90 那对刚好擦边过，最该报警的反而没报，改成 0.85
  - 14 条测试：归一化后同百分比走势得分相同、平坦窗口无答案、反转与动量方向相反、
    三个因子各自防前视、默认不含指标因子、拼错即报错、相关性能识别「换过刻度的双胞胎」
- 指标曲线：单只标的的历史走势，四个面板分开画（价格与布林带 / RSI / MACD / KDJ）。
  - `GET /api/indicators/{symbol}/series`，`days` 5–500，越界给 422 而不是静默夹取
  - 面板按**指标族**取字段（`rsi` 而非 `rsi_14`）：把 `rsi_period` 改成 21，曲线换一条
    而不是整张图空掉。`family_of()` 因此在 `indicators.py` 里收敛成一处，目录查询与
    面板取字段共用
  - 预热期的空值发 `null`，前端**断线**而不是画到底部——把 `NaN` 当 0 会让 RSI 开头
    凭空出现一段暴跌
  - MACD 柱画成柱（看的是哪一侧，不是斜率）；RSI/KDJ 画出 20/80 参考线，且参考线参与
    纵轴范围计算，否则行情远离阈值时那条线正好消失
  - 顺手补掉 `chart()` 的两个既有缺陷：孤立点（只有一天有值）画不出来，现在画成圆点；
    全为空值的窗口原本会算出 `Infinity` 并画出空图，现在给一句说明
  - 9 条契约测试，含「改周期后仍能找到字段」与「预热期是 null 不是 0」
- 指标界面：工作台新增第六个视图「指标」——把上一版实现的指标接到界面上，并保证
  「每个数字都能被解释」。
  - `GET /api/indicators` 返回每个指标最后一根 K 线的截面（中位数/区间）与说明；
    `GET /api/indicators/{symbol}` 返回单只标的的全部指标值
  - 说明文字取自 `indicators.catalog()`，与 `docs/INDICATORS.md` 同源——否则代码改了
    口径、界面还在念旧词，这是最难发现的一类不一致
  - 标签带周期（`RSI(14)` 而不是「RSI」）：只写指标名的界面让人分不清 14 和 6
  - **价格量纲不显示截面区间**：MA5 原本显示 `3.28 ~ 1356.54`，那不是行情，只是不同
    价位的票被放在一起求了个范围；留空比给一个看起来像信息的数字好
  - 首次打开自动算一次（复用面板缓存），之后读盘；只有点「重算」才重跑
  - 给 6 个缺「误读提醒」的指标补齐（MACD·DEA、BOLL 中/下轨、KDJ·D、+DI、−DI）
- 选股界面（P8 的 S6）：界面新增「建议」视图——打分表单提交为后台任务，名单卡显示
  排名/分数/等权权重，复盘卡回放历史名单。要点：
  - 名单卡同时写「名单日期」与「数据截至日」：盘前打开时前者是昨天，没有日期的名单
    最容易被误读
  - 回放（`as_of`）不写台账——那是关于过去的问题，不是当天持有的意见
  - 数据源没有落盘数据给 400 并说明，而不是让 `build_panel_cache` 抛 `ValueError` 变 500
  - `JobOut.kind` 的 Literal 同步加了 `"screen"`：漏了这一处，前端收不到这类任务
- 尾盘编排（P8 的 S2）：`quant-nightly` 一条命令串起「等到点 → 收数 → 增量刷新缓存 →
  打分 → 归档」，`--daemon` 每个交易日跑一次。关键是收数后先确认目标交易日真的落在
  数据里，没有就明确拒绝打分并写明最新到哪天：带着半天的数据打分，产出的名单看起来
  完整、实际是错的，还会写进台账拖累几个月的复盘数字。`--now` 跳过等待但仍然校验
- 选股台账与复盘（P8 的 S5）：`scoring/ledger.py` 按天归档选股记录（同一天重复运行
  覆盖而非追加），`review()` 回放历史名单，按「次日开盘买入、持有 N 个交易日卖出」
  计分，与回测的成交假设一致；持有期未走完的记录不参与统计，避免用半天的数据拉偏
  均值。新增 `quant-screen` 命令：`--review`/`--horizon` 看胜率，`--as-of` 回放历史，
  `--min-amount` 卡流动性，`--no-record` 只看看不落账
- 全量下载：采集请求支持 `all_symbols`（界面「范围 → 全市场」，CLI 仍为 `--all`），
  从数据源拉取全部标的；全市场运行强制 ≥0.35 秒间隔，任务的进度总量在清单拉取完成后
  才校准，返回结果附带 `swept`（标的数与实际节流间隔）。
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
