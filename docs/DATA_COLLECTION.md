# 数据收集 Data Collection

`quant-collect` 把 A 股日频 K 线落到本地磁盘，支持单次运行与常驻守护两种模式。
设计目标只有三条：**可独立运行、失败可恢复、落盘数据能直接喂给研究栈**。

## 快速开始

```bash
pip install -e '.[collect]'          # akshare + pyarrow

# 单次：指定标的
quant-collect --symbols 600519.SH,000001.SZ --start 2024-01-01

# 全市场（会先拉股票列表，慢，可能几十秒）
quant-collect --all --start 2020-01-01

# 离线演示：不联网、不装 akshare 也能跑通整条链路
quant-collect --provider mock --symbols 600000.SH --start 2024-01-01

# 常驻：每个交易日 18:30 自动收集
quant-collect --daemon --at 18:30
```

三种调用方式等价：

```bash
quant-collect --symbols 600519.SH
python -m quant_research.cli --symbols 600519.SH
python scripts/collect_a_shares.py --symbols 600519.SH   # 无需 pip install -e
```

## 落盘结构

```
data/raw/akshare/
    daily/600519.SH.parquet     # 每标的一个文件，date 索引
    daily/000001.SZ.parquet
    _state.json                 # 已收集区间、上次运行、失败清单
    _calendar.csv               # 可选：缓存的交易日历
reports/collection/
    20261021T183001_akshare.json
```

`data/raw/` 已被 `.gitignore` 排除：原始数据可由源重建，不应进版本库。

**每行字段**（标准英文列，`float64`）：

| 列 | 必需 | 说明 |
| --- | --- | --- |
| `date` | ✅ | 索引，已归一化到当日零点、升序、唯一 |
| `open` `high` `low` `close` | ✅ | 价格，随 `--adjust` 确定复权口径 |
| `volume` | ✅ | 成交量，**保留源单位**（A 股源为「手」），不做隐式换算 |
| `amount` | ⭕ | 成交额，元 |
| `pct_change` | ⭕ | 涨跌幅，% |

不做单位换算是有意的：科创板一手不是 100 股，任何"统一换算"都会写进看不见的假设。
成交量单位由 `_state.json` 的 `volume_unit` 记录。

优先 Parquet；没有 `pyarrow` 时自动降级 CSV，并清掉同名的旧格式文件，避免两份数据并存。

## 增量与复权

每次运行只请求 `上次最后日期 - lookback_days` 到今天。**这不是偷懒**：前复权价格在分红送股后会被
供应商回溯改写，重读一小段尾部能让近期历史与最新口径一致，而不必每晚重写多年数据。

```bash
quant-collect --symbols 600519.SH --full          # 忽略已存区间，全量重拉
quant-collect --symbols 600519.SH --lookback-days 30
```

复权口径写入 `_state.json.adjust`（`qfq` / `hfq` / `none`）。加载器读取时会把它映射为
`DataMeta.price_adjustment`（`forward` / `backward` / `none`），因此报告里的口径描述与数据一致。

## 调度

两种方式，选一个：

```bash
# 常驻进程
quant-collect --daemon --at 18:30 --timezone Asia/Shanghai

# 或者交给系统调度器（更省心，进程挂了不影响下一次）
30 18 * * 1-5  cd /path/to/quant-trading && .venv/bin/python scripts/collect_a_shares.py
```

守护模式的触发条件是：**到点 + 是交易日 + 当天没有成功运行过**。`_state.json` 的 `last_run_date`
只在没有失败时更新，所以一次失败的运行会在下一次调用时自然重试。

建议在收盘后运行（15:00 之后）。盘中调用拿到的是当日最新快照，`close` 并非最终收盘价。

### 交易日历

三级降级，不会因为日历服务挂掉而阻塞收集：

1. 没有缓存 —— 只跳过周末，节假日会被当作交易日（拉到空数据，无害）；
2. `data/raw/<provider>/_calendar.csv` —— 精确，离线可用；
3. `--refresh-calendar` —— 从 AKShare 下载并写缓存（慢、可能卡住，因此不是默认行为）。

日志会明确写出当前用的是哪一级。

## 故障处理

| 场景 | 行为 |
| --- | --- |
| 单个标的失败 | 记入 `report.failed` 与 `_state.json.failures`，其余标的继续 |
| 网络抖动 | 每个标的最多重试 `--retries` 次，指数退避 |
| 请求过快 | `--pause` 控制标的间隔，默认 0.5 秒 |
| 退出码 | `0` 全部成功；`1` 有标的失败；`2` 用法或配置错误 |

补数据：

```bash
quant-collect --symbols 600519.SH --start 2025-01-01 --end 2025-06-30   # 指定区间补
quant-collect --symbols 600519.SH --full                                # 整段重拉
```

> 已知限制：AKShare 的「全市场列表」接口（`stock_info_a_code_name`）偶尔长时间无响应。
> 因此 `--all` 失败时会回退到**已存储的标的清单**继续增量，而不是中断。日常请用
> `--symbols` 或 `config/settings.yaml` 的 `collection.symbols` 固化清单。

## 读取落盘数据

```python
from quant_research.data import load_price_panel, describe_panel

panel = load_price_panel("data/raw/akshare/daily")  # 目录布局，自动识别 parquet
panel.prices.tail()
panel.meta.price_adjustment  # 'forward' —— 来自 _state.json，不是配置默认值
describe_panel(panel).to_dict()
```

## 配置项

`config/settings.yaml` 的 `collection` 段（全部可用命令行覆盖）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `provider` | `akshare` | `akshare` 或 `mock` |
| `symbols` | `[]` | 空则由 `--symbols` / `--all` / 已存清单决定 |
| `output_directory` | `data/raw` | 存储根目录 |
| `format` | `parquet` | `parquet` 或 `csv` |
| `adjust` | `qfq` | `qfq` / `hfq` / `none` |
| `lookback_days` | `10` | 每次重读的尾部窗口 |
| `schedule_at` | `18:30` | 守护模式触发时间 |
| `timezone` | `Asia/Shanghai` | 调度时区 |
| `retries` | `2` | 单标的重试次数 |
| `pause_seconds` | `0.5` | 标的间隔 |
| `report_directory` | `reports/collection` | 运行报告归档位置 |

## 已验证

- AKShare 1.19.1 + `stock_zh_a_hist`：列名为 `日期/开盘/收盘/最高/最低/成交量/成交额/涨跌幅`，已按此映射；
- 真实冒烟：3 个标的（600519.SH、000001.SZ、300750.SZ）各 31 行，写入后
  `load_price_panel` 读回 `31 x 3`、`price_adjustment='forward'`；
- 重复运行同一区间：`+0 rows`，无重复行；
- 测试覆盖 36 条收集相关用例，全部使用离线 provider，不依赖网络。

## 明确不做

- 分钟线、财务数据、实时行情、龙虎榜等其它数据集（当前只做日频 K 线）；
- 跨源交叉校验与自动清洗 —— 那属于 `data/processed/` 的职责，见 [DATA.md](DATA.md)；
- 采集任务的分布式调度与队列。
