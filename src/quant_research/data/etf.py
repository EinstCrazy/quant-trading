"""ETF 日线快照：宽基与主流行业/跨境/商品 ETF 的独立清单与采集。

ETF 是独立资产类型，不是「另一种股票」，三件事说清楚：

1. **独立落盘**：`data/processed/etf/`——刻意不进
   `collection.output_directory`，否则 `_discover_datasets` 会把它当成
   股票数据集，混进回测/指标的「样本合计」大盘口径。
2. **没有财报红旗**：fundamentals 核对对 ETF 不适用（没有季报可查），
   核对端点会跳过 ETF 并如实标注「不参与核对」——这不是缺失，是豁免。
3. **清单写死且透明**：宽基覆盖主要宽基指数，另配主流行业/跨境/商品，
   便于回答「今天的环境适合动哪一类资产」。代码序没有排名含义，
   加减清单在 git 里留痕。

量比口径与 volume._window_stats 一致：当日成交额 / 前 20 日均额
（shift(1) 排除当日——今天的放量不能抬高自己的基线）。水位（现价 /
近 250 日最高，含当日）刻意不同：今天创 52 周新高，水位就该是 100%。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.data.providers.base import (
    ProviderError,
    normalise_bars,
    to_bare_symbol,
)

__all__ = [
    "DIRECTORY",
    "ETF_UNIVERSE",
    "fetch",
    "is_etf",
    "load",
    "meta",
    "refresh",
    "snapshot",
    "store",
]

ETF_UNIVERSE: dict[str, str] = {
    "510050": "上证50ETF",
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "512100": "中证1000ETF",
    "588000": "科创50ETF",
    "159915": "创业板ETF",
    "159949": "创业板50ETF",
    "512880": "证券ETF",
    "512010": "医药ETF",
    "515000": "科技ETF",
    "512690": "酒ETF",
    "518880": "黄金ETF",
    "513100": "纳指ETF",
    "513500": "标普500ETF",
}

# 场内基金代码段（与 normalize_symbol 的前缀映射一致）。
_ETF_PREFIXES = ("50", "51", "58", "15", "16", "18")

DIRECTORY = Path("processed/etf")

_COLUMN_MAP: dict[str, str] = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "换手率": "turnover",
}


def is_etf(symbol: str) -> bool:
    """按裸代码前缀判定场内基金——红旗核对据此豁免，快照据此归类。"""
    return to_bare_symbol(symbol).zfill(6)[:2] in _ETF_PREFIXES


def _path(root: Path, symbol: str) -> Path:
    return root / DIRECTORY / f"{to_bare_symbol(symbol)}.parquet"


def fetch(
    symbol: str,
    adjust: str = "qfq",
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """从东财拉 ETF 日线（fund_etf_hist_em），映射成标准 bar 列。"""
    import akshare as ak

    bare = to_bare_symbol(symbol)
    start_text = pd.Timestamp(start).strftime("%Y%m%d") if start else "19900101"
    end_text = pd.Timestamp(end or date.today()).strftime("%Y%m%d")
    try:
        raw = ak.fund_etf_hist_em(
            symbol=bare,
            period="daily",
            start_date=start_text,
            end_date=end_text,
            adjust=adjust,
        )
    except Exception as exc:
        raise RuntimeError(f"ETF {bare} 抓取失败：{type(exc).__name__}: {exc}") from exc
    if raw is None or raw.empty:
        raise ValueError(f"ETF {bare} 没有返回数据")
    missing = {"日期", "开盘", "收盘", "最高", "最低", "成交量"} - set(raw.columns)
    if missing:
        raise ValueError(f"ETF {bare} 返回缺少列：{sorted(missing)}")
    frame = raw.rename(columns=_COLUMN_MAP).set_index("date")
    return normalise_bars(frame, source=f"etf:{bare}")


def store(root: Path, symbol: str, frame: pd.DataFrame) -> Path:
    target = _path(root, symbol)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target)
    meta_info = {
        "symbol": to_bare_symbol(symbol),
        "name": ETF_UNIVERSE.get(to_bare_symbol(symbol), ""),
        "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": len(frame),
        "as_of": str(frame.index[-1].date()) if len(frame) else None,
    }
    target.with_suffix(".json").write_text(
        json.dumps(meta_info, ensure_ascii=False), encoding="utf-8"
    )
    return target


def load(root: Path, symbol: str) -> pd.DataFrame | None:
    target = _path(root, symbol)
    if not target.is_file():
        return None
    try:
        frame = pd.read_parquet(target)
    except Exception:  # noqa: BLE001 - 坏文件按缺缓存处理，与分析路径一致
        return None
    return None if frame.empty else frame


def meta(root: Path, symbol: str) -> dict[str, Any] | None:
    target = _path(root, symbol).with_suffix(".json")
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def refresh(root: Path, symbol: str, adjust: str = "qfq") -> pd.DataFrame:
    """联网抓取并落盘，返回抓到的帧。

    超长历史的 qfq 有个东财复权算法的坑：低价 ETF 的早期价格可能被
    折成负数（510050 在 2005 年附近的复权收盘是负值），normalise_bars
    会正确拒绝。这种失败改从 2015 年起重试——十年的日线足够算水位与
    量比，比「要么全历史要么没有」诚实且有用。
    """
    try:
        frame = fetch(symbol, adjust=adjust)
    except (ValueError, ProviderError):
        frame = fetch(symbol, adjust=adjust, start=date(2015, 1, 1))
    store(root, symbol, frame)
    return frame


def _row(frame: pd.DataFrame) -> dict[str, Any]:
    """一只 ETF 的状态行：价位与量能，口径见模块 docstring。"""
    close = frame["close"]
    amount = frame["amount"] if "amount" in frame.columns else None
    as_of = str(close.index[-1].date())
    day_change: float | None = None
    vol_ratio: float | None = None
    if len(close) >= 2:
        day_change = float(close.iloc[-1] / close.iloc[-2] - 1)
    if amount is not None and len(amount) >= 21:
        baseline = amount.iloc[-21:-1].mean()
        if baseline and not pd.isna(baseline):
            vol_ratio = float(amount.iloc[-1] / baseline)
    window = close.iloc[-250:]
    level = float(close.iloc[-1] / window.max()) if len(window) and window.max() else None
    ma20 = close.iloc[-20:].mean() if len(close) >= 20 else None
    ma20_gap = float(close.iloc[-1] / ma20 - 1) if ma20 else None
    return {
        "as_of": as_of,
        "last_close": float(close.iloc[-1]),
        "day_change": day_change,
        "vol_ratio": vol_ratio,
        "level": level,
        "ma20_gap": ma20_gap,
    }


def snapshot(root: Path, adjust: str = "qfq") -> dict[str, Any]:
    """全部清单 ETF 的快照（只读缓存，不联网）。

    没采集过的代码进 ``missing`` 而不是悄悄消失——「没采」必须可见。
    """
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    as_of: str | None = None
    for bare, name in sorted(ETF_UNIVERSE.items()):
        frame = load(root, bare)
        if frame is None:
            missing.append(f"{bare} {name}")
            continue
        row = {"symbol": bare, "name": name, **_row(frame)}
        if as_of is None or row["as_of"] > as_of:
            as_of = row["as_of"]
        rows.append(row)
    return {
        "available": bool(rows),
        "as_of": as_of,
        "rows": rows,
        "missing": missing,
        "n_tracked": len(ETF_UNIVERSE),
        "adjust": adjust,
        "note": "量比=当日额/前20日均额（不含当日）；水位=现价/近250日最高（含当日，今日新高即100%）；MA20偏离=现价/20日均-1",
    }
