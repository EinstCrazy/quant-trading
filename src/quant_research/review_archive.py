"""复盘存档：复盘的本质是对比——不存档，昨天说的和今天看到的对不上。

每个数据日一份 JSON（文件名 = 复盘数据日 ``as_of``），同一天重复保存
覆盖：同一天的复盘以最后一次为准。存的是结论，不是行情——行情永远
从面板现读；这里只留「那天我看到的档位是什么」，用于下次打开时回答
「和上次比，什么变了」。

结构::

    {
      "date": "2026-11-07",
      "saved_at": "2026-11-09T16:40:12",
      "provider": "baostock",
      "summary_note": "2 只需要盯（止损/减仓观察）……",
      "positions": {
        "600519.SH": {"stance": "hold", "label": "持有",
                      "close": 1392.05, "pnl_pct": -0.57}
      }
    }

存档是显式动作（界面上点「存为今日复盘」），不是 GET 的副作用：
界面不该悄悄改写数据。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quant_research.logging_setup import get_logger
from quant_research.review import STANCE_LABELS

logger = get_logger("reviews")

__all__ = ["compact", "directory", "latest", "load", "save"]


def directory(root: Path | str) -> Path:
    return Path(root).expanduser() / "reviews"


def compact(payload: dict[str, Any]) -> dict[str, Any]:
    """裁剪成「那天的结论」：档位、收盘、盈亏率，不留冗余。"""
    positions: dict[str, dict[str, Any]] = {}
    for row in payload.get("positions") or []:
        account = str(row.get("account") or "默认")
        # 键带账户：同一票在两个账户各有一条档位，不能互相覆盖
        positions[f"{account}::{row['symbol']}"] = {
            "symbol": row["symbol"],
            "account": account,
            "stance": row["stance"],
            "label": STANCE_LABELS.get(row["stance"], row["stance"]),
            "close": row.get("close"),
            "pnl_pct": row.get("pnl_pct"),
        }
    summary = payload.get("summary") or {}
    return {
        "date": payload.get("as_of"),
        "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": payload.get("provider"),
        "summary_note": summary.get("note"),
        "positions": positions,
    }


def save(root: Path | str, payload: dict[str, Any]) -> Path:
    """Persist one day's review; re-saving the same day replaces it."""
    date = payload.get("as_of")
    if not date:
        raise ValueError("复盘数据没有日期（as_of），无法存档：先采集行情")
    folder = directory(root)
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / f"{date}.json"
    tmp = file.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(compact(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, file)
    return file


def load(root: Path | str, date: str) -> dict[str, Any] | None:
    file = directory(root) / f"{date}.json"
    if not file.is_file():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("复盘存档读不出来（%s）：%s", file.name, exc)
        return None


def latest(root: Path | str, *, exclude: str | None = None) -> dict[str, Any] | None:
    """The most recent archived review, optionally skipping one date.

    ``exclude`` 用来跳过「今天」——对比的对象应该是上一次，不是自己。
    """
    folder = directory(root)
    if not folder.is_dir():
        return None
    dates = sorted(
        (path.stem for path in folder.glob("*.json") if path.suffix == ".json"),
        reverse=True,
    )
    for date in dates:
        if date == exclude:
            continue
        data = load(folder.parent, date)
        if data:
            return data
        logger.warning("跳过损坏的复盘存档：%s", date)
    return None
