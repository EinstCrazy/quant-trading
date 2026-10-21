# 数据规范 Data

## 支持的输入格式

当前 `load_close_prices` 仅支持 CSV，且要求至少两列：

```csv
date,close
2024-01-02,100.25
2024-01-03,101.30
2024-01-04,99.80
```

| 列 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `date` | 日期 | ✅ | 会被 `parse_dates` 解析，支持大部分常见格式 |
| `close` | 数值 | ✅ | 收盘价，必须为正 |

列名可通过 `date_column` 与 `price_column` 参数自定义。

## 加载时校验

`load_close_prices` 返回前会执行四项检查，任一失败即抛 `ValueError`：

1. 必需列是否存在
2. 日期列是否重复
3. 收盘价是否缺失
4. 收盘价是否为正

返回值保证：索引唯一、无缺失值、价格为正、按日期升序。

## 目录用途

| 目录 | 存放内容 | 入库 |
| --- | --- | --- |
| `data/raw/` | 未经修改的原始下载文件 | ❌ |
| `data/processed/` | 清洗、对齐、复权后的数据 | ❌ |
| `data/cache/` | 派生缓存（Parquet 等） | ❌ |
| `data/sample/` | 合成样例数据（固定种子可重建） | ✅ |

`raw` / `processed` / `cache` 均可由上游重建，因此不提交版本控制；`sample` 体量小且用于让 CI 与新环境直接运行，故入库。

`load_price_panel` 支持三种输入布局：长表 CSV（含 `symbol` 列）、单标的 CSV、目录（`*.csv`，每文件一个标的）。
`open` / `high` / `low` / `volume` 若存在，会随 `PricePanel.extra` 一起传递。

## 数据元信息要求

每次实验必须记录以下信息，建议写入实验配置或报告：

| 项 | 说明 | 示例 |
| --- | --- | --- |
| 数据来源 | 供应商或文件来源 | `local csv` / 供应商名称 |
| 样本区间 | 起止日期 | `2015-01-01 ~ 2024-12-31` |
| 时区 | 时间戳时区 | `UTC` |
| 复权方式 | 前复权 / 后复权 / 不复权 | `adjusted` |
| 频率 | 日 / 周 / 月 | `daily` |
| 缺失值处理 | 填充或剔除策略 | `ffill limit=2` |

缺少这些元信息的结果不可复现，不应作为决策依据。

## 复权处理

不同复权方式直接影响收益计算：

- **前复权**：以最新价格为基准调整历史价格，适合看技术形态。
- **后复权**：以最早价格为基准调整后续价格，适合计算累计收益。
- **不复权**：原始成交价，会在除权除息日产生虚假跳空。

`config/settings.yaml` 中 `data.price_adjustment` 会被写入 `DataMeta.price_adjustment`。
加载器**不会**自动复权，使用前请确保源数据本身已按要求处理——否则除权日的跳空会被
`describe_panel` 标记为异常跳变，也会污染收益与回撤。

> 注意：若使用不复权价格计算收益，除权日会出现大幅虚假下跌，
> 并被 `max_drawdown` 记录为真实回撤。

## 缺失值与停牌

`missing_policy="raise"`（默认）**直接拒绝**缺失值，这是一条保守且安全的策略。
需要先观察数据问题时用 `missing_policy="keep"`，缺失会保留为 `NaN` 并出现在
`describe_panel` 的缺失率中；回测引擎遇到缺失价当日会跳过该标的的交易，
并记 `skipped_symbol` 警告。

以下处理仍未实现，需要时请自行在 `data/processed/` 阶段完成：

- 停牌期间前向填充，但设置填充上限
- 上市首日及之前的数据剔除
- 跨标的对齐到统一交易日历

## 样例数据

仓库内置 `data/sample/sample_prices.csv`（3 个合成标的、约 504 个交易日、含 OHLCV），
由固定随机种子生成，可完全重建：

```bash
python scripts/generate_sample_data.py
```

加载方式与真实数据一致：

```python
from quant_research.data import load_price_panel

panel = load_price_panel("data/sample/sample_prices.csv")
panel.prices.tail()  # date × symbol 收盘价
panel.field("open")  # 开盘价，供次日开盘成交假设使用
panel.meta.to_dict()  # 来源、区间、复权口径
```

详见 [../data/sample/README.md](../data/sample/README.md)。真实数据放入 `data/raw/`（不入库）。

## 相关文档

- [ROADMAP.md](ROADMAP.md) — P1 数据层建设任务
- [ARCHITECTURE.md](ARCHITECTURE.md) — 数据流与分层设计
- [API.md](API.md) — `load_close_prices` 详细说明
- [CONVENTIONS.md](CONVENTIONS.md) — 研究约定与回测防坑清单
