# 样例数据 Sample Data

`sample_prices.csv` 是**合成数据**，不是真实行情。

它的唯一用途是让 README 示例、测试与回测引擎在离线环境下可直接运行。该数据不含任何真实标的信息，**不得**用于任何研究结论。

| 项 | 值 |
| --- | --- |
| 生成脚本 | `scripts/generate_sample_data.py`（固定随机种子 `20240101`，可完全重建） |
| 格式 | 长表：`date,symbol,open,high,low,close,volume` |
| 标的 | `ALPHA`、`BETA`、`GAMMA`（均为合成） |
| 区间 | 约 504 个交易日，自 2022-01-03 起的工作日 |
| 频率 | 日频 |

重建命令：

```bash
python scripts/generate_sample_data.py
```

加载方式：

```python
from quant_research.data import load_price_panel

panel = load_price_panel("data/sample/sample_prices.csv")
panel.prices.tail()  # date × symbol 收盘价面板
panel.field("open")  # 开盘价面板，供次日开盘成交假设使用
panel.meta.to_dict()  # 数据来源与元信息
```

真实数据应放入 `data/raw/`（不入库）；样例数据体量小且可重建，故随仓库提交，以便 CI 与新环境直接运行。
