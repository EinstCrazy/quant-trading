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
    "sh000001": "上证指数",
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


def meta(root: Path, symbol: str) -> dict[str, Any] | None:
    target = _path(root, symbol).with_suffix(".json")
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
