"""大盘指数日线：让「大盘量能」名副其实。

此前量能页的「大盘」是已落盘样本的合计成交额——样本只有几十只时，它反
映的是样本资金活跃度，不是全市场。本模块把真实指数日线（收盘、成交额）
落到 `data/processed/index/`，量能分析优先走指数、缺缓存时退回样本合计
并如实标注来源。

三条约定与外围快照一致：
- GET 只读缓存，不联网——刷新指数不该是页面刷新的副作用；
- 联网抓取只发生在显式动作（补数 job、nightly）里；
- 抓取是全量重拉（8 千多行、一次请求），不做增量——增量省下的秒数换来
  的复权与对齐风险不值得。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

__all__ = ["DIRECTORY", "INDEXES", "fetch", "load", "refresh", "store"]

INDEXES: dict[str, str] = {
    # 顺序有意：第一个是「大盘量能」的主口径（全市场成交额），
    # 其余是结构对照——只涨上证不涨中证/创业板，环境结论就不完整
    "sh000001": "上证指数",
    "sh000300": "沪深300",
    "sz399006": "创业板指",
    "sz399001": "深证成指",
}

DIRECTORY = Path("processed/index")


def _path(root: Path, symbol: str) -> Path:
    return root / DIRECTORY / f"{symbol}.parquet"


def fetch(symbol: str) -> pd.DataFrame:
    """从东财拉指数日线（date/close/amount），amount 单位为元。"""
    import akshare as ak

    frame = ak.stock_zh_index_daily_em(symbol=symbol)
    if frame is None or frame.empty:
        raise ValueError(f"指数 {symbol} 没有返回数据")
    frame = frame.rename(columns=str.lower)
    missing = {"date", "close", "amount"} - set(frame.columns)
    if missing:
        raise ValueError(f"指数 {symbol} 返回缺少列：{sorted(missing)}")
    out = frame[["date", "close", "amount"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    out = out.dropna(subset=["amount"]).set_index("date").sort_index()
    out.index = out.index.normalize()
    return out


def store(root: Path, symbol: str, frame: pd.DataFrame) -> Path:
    target = _path(root, symbol)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target)
    meta = {
        "symbol": symbol,
        "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": len(frame),
        "as_of": str(frame.index[-1].date()) if len(frame) else None,
    }
    target.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
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


def refresh(root: Path, symbol: str) -> pd.DataFrame:
    """联网抓取并落盘，返回抓到的帧。"""
    frame = fetch(symbol)
    store(root, symbol, frame)
    return frame


def compare(root: Path) -> dict[str, Any]:
    """全部 INDEXES 的对比快照（只读缓存，不联网）。

    没采过的指数进 missing——「没采」必须可见，不能让对比表
    静悄悄少一行、让人觉得那个指数「没涨没跌」。
    """
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for symbol, name in INDEXES.items():
        frame = load(root, symbol)
        if frame is None:
            missing.append(f"{name}({symbol})")
            continue
        close = frame["close"]
        day_change = float(close.iloc[-1] / close.iloc[-2] - 1) if len(close) >= 2 else None
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "last": float(close.iloc[-1]),
                "day_change": day_change,
                "as_of": str(close.index[-1].date()),
            }
        )
    return {
        "available": bool(rows),
        "rows": rows,
        "missing": missing,
        "note": "主口径：上证指数（量能分析的大盘成交额用它）；其余为结构对照",
    }


def meta(root: Path, symbol: str) -> dict[str, Any] | None:
    target = _path(root, symbol).with_suffix(".json")
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
