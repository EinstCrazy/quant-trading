# 技术指标 Indicators

**这份文档给实现者看：公式、参数、以及那些写错了不会报错、只会让结果悄悄偏掉的细节。**

它不是买卖指南。指标描述的是价格**已经**做了什么，不预测它接下来要做什么；文档里
出现的所有"超买""金叉"都只是约定俗成的叫法，不代表任何预测能力。

项目目前只用了四个截面因子（动量、均线距离、波动率倒数、流动性，见
`src/quant_research/scoring/factors.py`）。这份文档记录的是"如果要加，该怎么算"，
以及"为什么不能直接把指标当分数用"（最后一节）。

---

## 通用约定

- **输入是一张宽表**：`index=DatetimeIndex(date)`，`columns=标的代码`，一列一只标的。
  所有函数都接受宽表、返回宽表，索引与列都不动。
- **全部向量化**：`pandas` 的 `rolling` / `ewm` / `cumsum`，不逐行循环。全市场几千列，
  双层 `iterrows` 会把一次计算变成几分钟（`describe_panel` 已经踩过这个坑）。
- **每个值只看过去**：指标用当日及以前的数据；信号次日才能执行。回测与选股的成交假设
  必须一致，否则回测是幻觉。
- **不引入 TA-Lib**：编译依赖带来的麻烦大于收益，pandas 足够表达这些公式。
- **统一前复权（qfq）**：不同源的前复权基准不同，跨源混用会让任何跨期比较失去意义。

---

## 先说五个贯穿所有指标的坑

后面每个指标只写它特有的坑，这几条不再重复。

### 1. `ewm` 的默认值不是行情软件里的那个 EMA

`pandas` 的 `ewm(span=n)` 默认 `adjust=True`，前几根用加权平均修正，而 TA-Lib、
通达信、同花顺用的是递推式 `E_t = α·x_t + (1-α)·E_{t-1}`。两者在前几十根上肉眼可见地
不同（实测同一序列 `span=3`）：

```text
adjust=True  : 10.0000  10.6667  11.4286  11.2000  12.1290
adjust=False : 10.0000  10.5000  11.2500  11.1250  12.0625
```

想和行情软件对上，必须 `adjust=False`。

### 2. `ewm` 第一根 K 线就出值

`rolling(n)` 前 `n-1` 个是 `NaN`，但 `ewm(adjust=False)` 的第一个值就是它自己——
于是第 1 天就有一个"14 日 RSI"，而它其实只用了 1 根 K 线。

```text
rolling(5).mean()          : nan  nan  nan  nan  100.34  100.43
ewm(span=5, adjust=False)  : 100.13  ...（第一根就有值）
```

**所以每个 `ewm` 都要显式给 `min_periods`**，否则指标的"预热期"是假的，而它在回测里
表现为开头几十根异常的进出场信号——很难查，因为没有任何一行代码是错的。

### 3. 标准差的 `ddof`：布林带用总体，不是样本

`pandas` 的 `.std()` 默认 `ddof=1`（样本标准差），布林带的公式用的是总体标准差
`ddof=0`。`n=20` 时两者相差 **2.60%**（实测 3.2324 vs 3.3163），带宽整体偏宽，
突破信号会系统性来得更晚、更少。

### 4. 停牌、新股与一字板

- 停牌日没有行情 → 序列里是 `NaN` → `rolling` 窗口里有 `NaN` 就整段变 `NaN`。
  按"缺失策略"处理（本项目用 `missing_policy="keep"`），指标端用 `min_periods` 兜住。
- 新股上市不足 `n` 天 → 指标为 `NaN`，不是 0。选股时这条由 `short_history` 统计，
  不要让它掉进 `no_score`（那是另一回事）。
- 一字板（`open == high == low` 且近涨停）会让 KDJ、威廉%R 这类"区间位置"指标的分母
  为 0 → 除零。指标照算没问题，名单要剔除（选股层已经做）。

### 5. 指标是时序的，因子是截面的

RSI=70 对一只常年高波动的票和对一只大盘银行股，含义完全不同。**把指标的绝对值直接
拿去横向排序是错的**——必须先逐日标准化（`zscore_by_day`），这也是 `scoring` 里
逐日 z-score 而非全样本标准化的原因。详见最后一节。

---

## 趋势类

### MA / EMA

最简单，也最容易被用错：均线是滞后量，它说的是过去，不是未来。

```text
MA(n)  = 最近 n 根收盘价的均值
EMA(n) : E_t = 2/(n+1) · C_t + (1 - 2/(n+1)) · E_{t-1}
```

```python
ma = close.rolling(n).mean()
ema = close.ewm(span=n, adjust=False, min_periods=n).mean()
```

**常用参数**：5 / 10 / 20 / 60 / 120 / 250。
**坑**：`min_periods` 见上文；另外均线的"金叉死叉"在震荡市里会反复打脸，交易次数
比收益更值得先看一眼。

### MACD（12, 26, 9）

```text
DIF  = EMA(12) - EMA(26)
DEA  = EMA(DIF, 9)              # 信号线
柱   = 2 × (DIF - DEA)          # 国内软件画 2 倍；国外文献通常是 1 倍
```

```python
dif = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(
    span=26, adjust=False, min_periods=26
).mean()
dea = dif.ewm(span=9, adjust=False, min_periods=9).mean()
hist = 2 * (dif - dea)
```

**坑**：
- 柱子 1 倍还是 2 倍，决定了"零轴附近"的视觉尺度，也决定了很多网上抄来的阈值能不能
  直接用。写文档时注明用哪一种，别让读者猜。
- DIF 依赖 EMA(26)，前 26 根是 `NaN`——MACD 的预热期比多数人以为的长。

### DMI / ADX

衡量趋势的**强度**，不判断方向。

```text
+DM = 当日最高价高于昨高的部分（为负记 0），-DM 同理
TR  = max(H-L, |H-昨收|, |L-昨收|)
+DI = 100 × Wilder平滑(+DM) / Wilder平滑(TR)
-DI = 100 × Wilder平滑(-DM) / Wilder平滑(TR)
DX  = 100 × |+DI - -DI| / (+DI + -DI)
ADX = DX 的 Wilder 平滑（通常再取 14）
```

**坑**：`+DI + -DI = 0` 时分母为 0（全天无波动，一字板）→ 需保护；
ADX 本身滞后，它上升只说明"趋势正在变强"，不说方向。

---

## 震荡类

### RSI（14）

```text
delta = C_t - C_{t-1}
平均涨幅 = 上涨幅度的 Wilder 平滑（alpha = 1/n）
平均跌幅 = 下跌幅度（取绝对值）的 Wilder 平滑
RS  = 平均涨幅 / 平均跌幅
RSI = 100 - 100 / (1 + RS)
```

```python
delta = close.diff()
gain = delta.clip(lower=0)
loss = (-delta).clip(lower=0)
avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
rsi = 100 - 100 / (1 + avg_gain / avg_loss)
```

**坑**：
- **Wilder 平滑 vs 简单均值是两种 RSI**（Cutler's RSI）。数值不同，阈值不同。
  上面是 Wilder 版（也是 TA-Lib 与国内软件的默认）。
- **平均跌幅为 0 → 除零，RSI 应为 100**，不是 `inf`。同理平均涨幅为 0 时 RSI = 0。
  `pandas` 不会替你处理，回测里会污染后续所有计算。
- RSI 在强趋势里会长期"超买"——把它当反转信号用，是它最常见的误用。

### KDJ（9, 3, 3）

```text
RSV = (C - L9) / (H9 - L9) × 100        # H9/L9 = 9 日内最高/最低
K   = 2/3 × 前K + 1/3 × RSV             # 初值 50
D   = 2/3 × 前D + 1/3 × K               # 初值 50
J   = 3K - 2D
```

**坑**：
- K 和 D 是**递推**的，且**初值是 50 而不是第一个 RSV**。
  `ewm(alpha=1/3, adjust=False)` 的第一项是 RSV 本身，与"初值 50"不同——要么循环，
  要么把首值改掉。这是 KDJ 与 RSI 在写法上最容易出错的差别。
- `H9 == L9`（一字板、停牌后复牌）→ 分母为 0，RSV 未定义。
- J 值可以超出 0~100，这不是 bug。

### 威廉 %R（14）

```text
%R = (H_n - C) / (H_n - L_n) × (-100)     # 取值 -100（最低）到 0（最高）
```

**用途**：与 KDJ 的 RSV 同源，只是坐标相反、没有平滑。
**坑**：区间长度为 0 时同样除零；不同资料对符号约定不一致，落笔时写明区间是
`-100~0` 还是 `0~100`。

### CCI（14）

```text
TP = (H + L + C) / 3
MA = TP 的 n 日均值
MD = |TP - MA| 的 n 日均值            # 平均绝对偏差，不是标准差
CCI = (TP - MA) / (0.015 × MD)
```

**坑**：`MD` 是**平均绝对偏差**而非标准差，写错后 CCI 的尺度整体不对，而形状看起来
"很像"，极难发现；`0.015` 这个系数是为了让约 70% 的值落在 ±100 内，是约定值。

---

## 波动率类

### ATR（14）

```text
TR  = max(H - L, |H - 昨收|, |L - 昨收|)
ATR = TR 的 Wilder 平滑（alpha = 1/n）
```

```python
prev = close.shift(1)
tr = pd.concat(
    [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
).max(axis=1)
atr = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
```

**用途**：止损距离、`inverse_volatility` 权重的输入。它比"收益率的滚动标准差"好，因为
用到了最高最低价，对跳空也敏感。
**坑**：`high`/`low` 缺失时 `concat` 会给出全 `NaN` 的一行——需要 `min_periods` 与
缺失策略配合。

### 历史波动率（年化）

```text
日收益 = close.pct_change()
已实现波动率 = 日收益.rolling(n).std(ddof=1) × sqrt(252)
```

**坑**：这里用 `ddof=1`（样本）是对的——它是在**估计**总体波动率，与布林带描述
"这一段价格的离散程度"（`ddof=0`）不是同一件事。年化系数 252 是 A 股一年约 242~244
个交易日的惯例近似，跨市场比较时统一即可。

### 布林带 BOLL（20, 2）

```text
中轨 = MA(20)
上轨 = 中轨 + k × σ          # σ 用总体标准差 ddof=0，k 通常取 2
下轨 = 中轨 - k × σ
带宽 = (上轨 - 下轨) / 中轨   # 衡量"挤不挤"，比位置更有信息量
```

```python
mid = close.rolling(n).mean()
spread = close.rolling(n).std(ddof=0)      # 不是 pandas 默认的 ddof=1
upper, lower = mid + k * spread, mid - k * spread
```

**坑**：`ddof`（见上文，2.60% 的带宽差）；带宽收窄只说明波动变小，不说方向。

---

## 成交量类

### OBV

```text
若 C > 昨收：OBV += 成交量
若 C < 昨收：OBV -= 成交量
其余：不变
```

```python
direction = close.diff().fillna(0.0)
obv = (volume * direction.map(lambda v: 0.0 if v == 0 else (1.0 if v > 0 else -1.0))).cumsum()
```

**坑**：OBV 是**累计量**，绝对值取决于起点，只能看趋势、不能横向比较；除权除息日的
量价关系会失真（前复权改的是价格，成交量不动）。

### VWAP（成交均价）

```text
VWAP = 成交额 / 成交量
```

**坑（单位）**：本项目 `volume` 存的是**手**，`amount` 是**元**，所以
`VWAP = amount / (volume × 100)`。少乘这个 100，均价会贵 100 倍，而曲线形状完全
正常——这是单位错误里最难发现的一类。各 provider 原始单位不同（BaoStock 报股、
AKShare 报手），换算已在 provider 层统一，新增数据源时别再改回去。

### 换手率与流动性

```text
换手率 = 成交量 / 流通股本
```

流通股本不在日线里（需要基本面数据），所以选股层用**成交额的滚动均值**做流动性过滤
（`min_amount`）——这是可得数据里的合理替代。`scoring/factors.py` 的流动性因子与
`screen(min_amount=...)` 已经是这么做的。

---

## 指标 ≠ 因子：为什么不能直接拿 RSI 排序

这是把上面的指标接进选股时最容易犯的错，也正是 `scoring` 层存在的原因。

| | 技术指标（RSI / MACD / BOLL） | 截面因子（`scoring/factors.py`） |
| --- | --- | --- |
| 方向 | **时序**：一只票沿着时间算 | **截面**：同一天所有票横向比 |
| 绝对值 | 跨标的不可比（各自的波动性格不同） | 逐日 z-score 后可比 |
| 典型误用 | 把 RSI=70 当"比 RSI=60 更强" | —— |

所以正确的接法是：**指标先算，再逐日标准化，再进组合**。而且逐日标准化不能换成
"全样本一次性标准化"——后者会让周一的分数依赖全年走势，是前视偏差。

`scoring` 已经有四个因子（动量、均线距离、波动率倒数、流动性）。要加 RSI 或 MACD
作因子时：

1. 在 `scoring/factors.py` 里加一个向量化函数，输入 `PricePanel`、输出宽表，
   每个值只看过去；
2. 交给 `combine()` 做逐日 z-score 与加权；
3. 补两条测试：手算小样本核对、以及**防前视自检**（截断历史重算，同一天的结果必须
   完全一致）——这条自检是整个 `scoring` 的验收标准，别跳过。

## 实现约定（落地时照此办理）

- **签名统一**：`def xxx(panel, *, n=14, **kwargs) -> pd.DataFrame`，宽表进、宽表出，
  不改索引、不改列。
- **参数写默认值**，与行情软件惯例一致（MACD 12/26/9、RSI 14、BOLL 20/2、KDJ 9/3/3）。
- **`min_periods` 必须显式给**，尤其是所有 `ewm`。
- **除零全部显式处理**：RSI 的单边行情、KDJ/威廉的一字板、ADX 的无波动。
- **不要 `dropna()` 了事**：那会让每只标的的可比起点不同，回测的样本期悄悄变短。
- **测试三件套**：手算小样本、防前视自检、边界（`NaN`、除零、不足 `n` 根）。

## 参考

- Wilder, *New Concepts in Technical Trading Systems*（RSI、ATR、ADX 的原始出处）
- Bollinger, *Bollinger on Bollinger*（布林带；原文用总体标准差）
- 各指标在国内行情软件中的实现差异（MACD 柱的 1 倍/2 倍、KDJ 初值 50）以软件说明为准，
  本文档已标注分歧点
