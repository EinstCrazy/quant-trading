# 数据收集 Data Collection

`quant-collect` 把 A 股日频 K 线落到本地磁盘，支持单次运行与常驻守护两种模式。
设计目标只有三条：**可独立运行、失败可恢复、落盘数据能直接喂给研究栈**。

## 快速开始

```bash
pip install -e '.[collect]'          # akshare + baostock + pyarrow

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

## 数据源

同一个 `--provider` 开关切换，落盘结构与下游读取完全一致：

| provider | 取什么 | 适用 | 注意 |
| --- | --- | --- | --- |
| `akshare` | 沪深京日线 + 全市场列表 | 默认，覆盖面最广 | 抓取第三方公开接口，**上游改版就会失效**；全市场列表偶尔长时间无响应 |
| `baostock` | 沪深京日线 + 全市场列表 | akshare 挂掉时的备份 | 免费、无需 token、接口常年不变；需要 login/logout；成交量原始单位是**股** |
| `sse` | **仅**沪市官方证券清单 | 校准全市场清单 | 不提供行情：`fetch_daily` 直接报错，而不是返回空集 |
| `futu` | 沪深/港/美日线 + **实时快照** | 实时行情、盘口、下单 | 需本机跑 OpenD 网关；历史 K 线按「7 天内的标的数」限额，**不适合全市场**；成交量原始单位也是**股** |
| `mock` | 确定性随机游走 | 离线演示 | 不联网 |

### 不要在一个目录里混用两个源

前复权价格依赖"最新这一天"作为基准，各家基准不同，**同一天的收盘价可以相差百分之一以上**。
实测 600519 在 2024-01-02：

| 源 | 前复权收盘 | 成交量 |
| --- | --- | --- |
| akshare | 1550.67 | 32156 手 |
| baostock | 1531.23 | 32156 手（原文 3,215,644 股，已换算） |

成交量一致说明换算是准的；价格的差异不是错误，是复权基准不同。落盘目录按 provider 分隔
（`data/raw/<provider>/`），正是为了让这件事无从发生。

> 想从交易所直接拿历史日线此路不通：上交所不对个人免费提供，`sseinfo.com` 的授权数据是
> 付费服务。所以 `sse` 这个 provider 只做清单，价格由另外两个源负责。

### 富途 futu：实时行情可以，全市场历史不行

安装 `pip install -e '.[futu]'`，另外需要在本机启动 **OpenD** 网关（默认 `127.0.0.1:11111`）
并登录牛牛 / moomoo 账号（**登录无需开户**，但要完成问卷与协议确认）。

它的强项和弱项正好相反，先分清是哪一类需求：

| 需求 | 用 futu？ | 原因 |
| --- | --- | --- |
| 实时报价、盘口、逐笔 | **适合** | 接口本身免费，限频宽松（快照 30 秒 60 次），订阅额度 100 起 |
| 全市场日线历史 | **不适合** | 历史 K 线按「7 天内不同标的数」计量，见下表 |
| 下单 | 适合 | 通过 API 交易无附加收费，费率与 APP 一致 |

**历史 K 线额度（最近 7 天滚动，系统自动分配）**：

| 账户情况 | 订阅额度 | 历史 K 线额度 |
| --- | --- | --- |
| 总资产 < 1 万 HKD（**含未开户**） | 100 | **100** |
| ≥ 1 万 HKD | 300 | 300 |
| ≥ 50 万 HKD，或月交易 > 200 笔，或月交易额 > 200 万 HKD（任一） | 1000 | 1000 |
| ≥ 500 万 HKD，或月交易 > 2000 笔，或月交易额 > 2000 万 HKD（任一） | 2000 | 2000 |

同一只标的 7 天内重复拉取**不重复计费**，同一只的不同周期也只占 1 个额度。但 A 股全市场
约 5000 只：即便在 1000 那一档，覆盖一轮也要五周。**所以日线的全市场历史仍然用
baostock**，futu 用来补它没有的东西——实时、盘口、指数与板块、基本面、下单。

两个前提，缺一个就连不上或取不到数：

- **OpenD 必须在本机运行**（SDK 连的是网关，不是富途服务器）。
- **A 股 LV1 行情只对「境内认证客户」免费，判定依据是 OpenD 登录时的 IP**。
  在境外机器上跑，A 股直接不提供服务（港股 LV2、美股推广期 LV3 不受此限）。

```bash
# 历史日线（注意额度：每只标的消耗 1 个）
quant-collect --provider futu --symbols 600519.SH,000001.SZ --start 2024-01-01
```

实时快照不在采集链路里，是 provider 上的 `fetch_snapshot()`，供选股之后盯盘用。

> 这个 provider 的调用形态依据富途 OpenAPI 官方文档（v10.12），**尚未在本机连真实网关
> 验证**——OpenD 属于用户侧安装，仓库里没有也不该有。第一次用请先小批量试。

## 全量下载（全市场）

两处入口：

```bash
quant-collect --provider baostock --all --start 2015-01-01        # 命令行
```

界面：采集页「范围 → 全市场」；接口同样是 `POST /api/jobs/collect {"all_symbols": true}`。
两者都会先向数据源拉取全部标的（`list_symbols`），再逐只下载。

**它会自动放慢。** 几千只标的连着请求同一个主机，正是被判定为爬虫并直接切断的典型方式
（见上文「源拒绝连接时该怎么办」）。所以全量下载强制按 **≥0.35 秒** 的间隔逐只请求，
无论请求里传的 `pause` 是多少。实测数字（baostock，2026-10-28）：拉一次全市场清单
**30–42 秒**（7427 只），之后 7427 只按 0.35 秒节流——**整轮至少 50 分钟**。
这是刻意的取舍，不是可以调快的齿轮：跑得更快的代价通常是跑到一半被掐断，然后从头再来。

- **随时可取消**：它是后台任务，取消后已下载的照常落盘；下次运行按各自的 `last_date`
  续采，不会从头开始。
- **失败隔离**：单只失败不影响整批，失败标的与原因写在 `_meta.json` 里，界面「失败清单」
  可见。
- **选对源**：`sse` 只有清单没有行情，界面会拦住这个组合。
- **进度**：清单拉取期间显示「拉取全市场清单…」，总量要等清单拉回来才校准（所以一开始
  显示 1 个单位，不是卡住了）。这一阶段是同步调用，中途不可打断；取消会在清单返回后
  立即生效，此时一只都还没下载。

> 首次全量之后，日常增量只需跑同一条命令不带 `--all`：它会刷新已存标的并重抓尾部窗口。

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
| 源拒绝连接（疑似限流） | 归类为 `SourceUnreachable`：报告写明原因与出路，退避放慢到 5 倍 |
| 请求过快 | `--pause` 控制标的间隔，默认 0.5 秒 |
| 退出码 | `0` 全部成功；`1` 有标的失败；`2` 用法或配置错误 |

补数据：

```bash
quant-collect --symbols 600519.SH --start 2025-01-01 --end 2025-06-30   # 指定区间补
quant-collect --symbols 600519.SH --full                                # 整段重拉
```

### 源拒绝连接时该怎么办

AkShare 走的是东方财富的公开行情接口。当对方**直接断开、连 HTTP 状态码都不返回**时，
采集器把它归类为"源不可用"而不是"数据错误"，报告里写出可能原因与出路，并把重试间隔
放慢到正常的 5 倍——对着一个正在拒绝你的接口高频重试，只会让限流持续得更久。

看到这类提示时的处理顺序：

1. **换源**：`quant-collect --provider baostock --symbols 600519.SH`
   （BaoStock 与东财互不相干，通常照常可用）
2. **暂停**：别再连续重试，等限流自行解除（几十分钟到数小时不等）
3. **自查**（可选）：判断是单个数据接口被拒，还是整站不可达——

   ```bash
   curl -4 -o /dev/null -w "%{http_code}\n" "https://quote.eastmoney.com/"   # 200 说明 IP 没被整体封禁
   curl -4 -o /dev/null -w "%{http_code}\n" "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&fields1=f1&fields2=f51&klt=101&fqt=1&beg=20240101&end=20240110"
   ```

   页面返回 200 而数据接口无响应，就可以确定是**数据接口的限流**，不是网络或 IP 的问题。

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
| `provider` | `akshare` | `akshare` / `baostock` / `sse` / `mock` |
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
- BaoStock + `query_history_k_data_plus`：成交量原始单位为**股**，除以 100 换算为手后与 AKShare
  实数一致（2024-01-02 的 600519：3,215,644 股 → 32156 手，两边同为 32156）；停牌日
  `tradestatus=0` 已剔除；列表接口只对交易日有响应，非交易日按日回退最多 7 天；
- 上交所 `downloadStockListFile.do`（GBK、Tab 分隔，`stockType=1` 主板 + `8` 科创板）：
  实测 2009 只沪市标的；BaoStock `query_all_stock` 实测 7437 只；
- 真实冒烟：3 个标的（600519.SH、000001.SZ、300750.SZ）各 31 行，写入后
  `load_price_panel` 读回 `31 x 3`、`price_adjustment='forward'`；
- 重复运行同一区间：`+0 rows`，无重复行；
- 测试覆盖 36 条收集相关用例，全部使用离线 provider，不依赖网络。

## 明确不做

- 分钟线、财务数据、实时行情、龙虎榜等其它数据集（当前只做日频 K 线）；
- 跨源交叉校验与自动清洗 —— 那属于 `data/processed/` 的职责，见 [DATA.md](DATA.md)；
- 采集任务的分布式调度与队列。
