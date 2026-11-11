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
    "compare",
    "fetch",
    "fetch_spot",
    "history",
    "is_etf",
    "load",
    "load_spot",
    "meta",
    "refresh",
    "refresh_spot",
    "share_delta",
    "snapshot",
    "store",
    "store_spot",
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
# 份额快照按日留档：单日快照只有「是多少」，两次之间才有「在流入还是流出」
SPOT_DIRECTORY = DIRECTORY / "spot"

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


def history(root: Path, symbol: str, days: int = 250) -> dict[str, Any]:
    """一只 ETF 的历史切片：蜡烛、均线、成交额与区间统计。

    只读缓存（``load``），不联网——和快照同一纪律。缺缓存抛 ValueError，
    由 API 转成「先采集」而不是一个空图。

    统计口径都写进 ``note``：区间涨跌=首尾收盘、年化波动=日收益标准差
    ×√244、最大回撤=收盘对历史峰值的最大跌幅。份额只有两次快照之间的
    变化，不是时间序列——如实给出「最新 / 上一份 / 变化」，不画假的曲线。
    """
    bare = to_bare_symbol(symbol)
    frame = load(root, bare)
    if frame is None:
        raise ValueError(f"ETF {bare} 还没有落盘数据：先点「刷新（联网采集）」")
    window = frame.iloc[-days:]
    close = window["close"]
    candles = [
        [
            str(index.date()),
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
        ]
        for index, row in window.iterrows()
    ]
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()

    def series(values: pd.Series) -> list[list[Any]]:
        return [
            [str(index.date()), None if pd.isna(value) else round(float(value), 3)]
            for index, value in values.items()
        ]

    amounts: list[list[Any]] = []
    if "amount" in window.columns:
        amounts = [
            [str(index.date()), None if pd.isna(value) else round(float(value) / 1e8, 2)]
            for index, value in window["amount"].items()
        ]

    first, last = float(close.iloc[0]), float(close.iloc[-1])
    high_index = close.idxmax()
    low_index = close.idxmin()
    returns = close.pct_change().dropna()
    drawdown = (close / close.cummax() - 1).min() if len(close) else None
    amount_columns = window["amount"] if "amount" in window.columns else None
    stats: dict[str, Any] = {
        "rows": len(frame),
        "window_rows": len(window),
        "span": [str(frame.index[0].date()), str(frame.index[-1].date())],
        "change": round(last / first - 1, 4) if first else None,
        "last_close": round(last, 3),
        "high": {"value": round(float(close.max()), 3), "date": str(high_index.date())},
        "low": {"value": round(float(close.min()), 3), "date": str(low_index.date())},
        "volatility": round(float(returns.std()) * (244**0.5), 4) if len(returns) > 1 else None,
        "max_drawdown": None
        if drawdown is None or pd.isna(drawdown)
        else round(float(drawdown), 4),
        "avg_amount_20": None
        if amount_columns is None or len(amount_columns) < 20
        else round(float(amount_columns.iloc[-20:].mean()) / 1e8, 2),
        "avg_amount_window": None
        if amount_columns is None or not len(amount_columns)
        else round(float(amount_columns.mean()) / 1e8, 2),
    }
    share = share_delta(root).get(bare) or {}
    return {
        "symbol": bare,
        "name": ETF_UNIVERSE.get(bare, ""),
        "as_of": str(frame.index[-1].date()),
        "days": len(window),
        "candles": candles,
        "ma20": series(ma20),
        "ma60": series(ma60),
        "amounts": amounts,
        "stats": stats,
        "share": {
            "share": share.get("share"),
            "mktcap": share.get("mktcap"),
            "share_change": share.get("share_change"),
            "spot_as_of": share.get("spot_as_of"),
        },
        "note": (
            "区间涨跌=窗口首尾收盘；年化波动=日收益标准差×√244；"
            "最大回撤=收盘对窗口内峰值的最大跌幅；均额单位亿元；"
            "份额变化需要至少两次份额快照——只有最新值时如实留空"
        ),
    }


def compare(root: Path, symbols: list[str], days: int = 250) -> dict[str, Any]:
    """多只 ETF 归一化对比：首日=100 的收益曲线 + 各自统计 + 相关性。

    先按共同交易日对齐再归一化：不同 ETF 上市时间、停牌日都不同，不
    对齐就会出现「曲线上某天只有一部分标的有值」的假交叉。对齐后的窗口
    同时用于统计——画面与数字必须是同一段数据。

    只读缓存；没采过的进 ``missing`` 而不是让整张对比失败。
    """
    requested: list[str] = []
    for symbol in symbols:
        bare = to_bare_symbol(symbol)
        if bare and bare not in requested:
            requested.append(bare)
    if not requested:
        raise ValueError("至少选一只 ETF")

    closes: dict[str, pd.Series] = {}
    missing: list[str] = []
    for bare in requested:
        frame = load(root, bare)
        if frame is None:
            missing.append(f"{bare} {ETF_UNIVERSE.get(bare, '')}".strip())
            continue
        closes[bare] = frame["close"]

    if not closes:
        raise ValueError("选中的 ETF 都还没有落盘数据：先点「刷新（联网采集）」")

    # 内连接：只在全体都有成交的交易日上比，曲线不会因为某只停牌而错位
    aligned = pd.concat(closes, axis=1, join="inner").dropna(how="any").iloc[-days:]
    if len(aligned) < 2:
        raise ValueError("共同交易日不足两天：这些 ETF 的采集区间几乎不重叠")

    series: list[dict[str, Any]] = []
    stats: list[dict[str, Any]] = []
    for bare in [symbol for symbol in requested if symbol in closes]:
        window = aligned[bare]
        base = float(window.iloc[0])
        if not base:
            continue
        series.append(
            {
                "symbol": bare,
                "name": ETF_UNIVERSE.get(bare, ""),
                "points": [
                    [str(index.date()), round(float(value) / base * 100, 2)]
                    for index, value in window.items()
                ],
            }
        )
        returns = window.pct_change().dropna()
        drawdown = (window / window.cummax() - 1).min()
        stats.append(
            {
                "symbol": bare,
                "name": ETF_UNIVERSE.get(bare, ""),
                "change": round(float(window.iloc[-1]) / base - 1, 4),
                "volatility": round(float(returns.std()) * (244**0.5), 4)
                if len(returns) > 1
                else None,
                "max_drawdown": None if pd.isna(drawdown) else round(float(drawdown), 4),
                "first": str(window.index[0].date()),
                "last": str(window.index[-1].date()),
                "rows": len(window),
            }
        )

    labels = [row["symbol"] for row in series]
    correlations = aligned[labels].pct_change().dropna().corr() if len(labels) > 1 else None
    matrix = (
        [[round(float(correlations.loc[a, b]), 3) for b in labels] for a in labels]
        if correlations is not None
        else []
    )
    return {
        "days": len(aligned),
        "as_of": str(aligned.index[-1].date()),
        "series": series,
        "stats": stats,
        "correlation": {"symbols": labels, "matrix": matrix},
        "missing": missing,
        "note": (
            "曲线=各只收盘 / 窗口首日收盘 × 100，按共同交易日对齐后再算；"
            "波动=日收益标准差×√244；相关性用同一段对齐窗口的日收益"
        ),
    }


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


def fetch_spot() -> tuple[dict[str, dict[str, Any]], str]:
    """拉全部场内基金行情，过滤出清单里的 14 只。

    一次全表（东财分页约 20 秒）比逐只查便宜；只留清单内的行，
    未在清单里的 ETF 不进快照——清单外的代码不该悄悄出现在表里。
    """
    import akshare as ak

    raw = ak.fund_etf_spot_em()
    if raw is None or raw.empty:
        raise ValueError("ETF 现货行情返回为空")
    needed = {"代码", "最新价", "成交额", "最新份额", "总市值", "数据日期"}
    missing = needed - set(raw.columns)
    if missing:
        raise ValueError(f"ETF 现货行情缺少列：{sorted(missing)}")
    as_of = str(pd.to_datetime(raw["数据日期"].dropna().iloc[0]).date())
    rows: dict[str, dict[str, Any]] = {}
    for record in raw.itertuples(index=False):
        code = str(record.代码).zfill(6)
        if code not in ETF_UNIVERSE:
            continue
        share = record.最新份额
        mktcap = record.总市值
        rows[code] = {
            "price": _num_or_none(record.最新价),
            "amount": _num_or_none(record.成交额),
            "share": _num_or_none(share),
            "mktcap": _num_or_none(mktcap),
        }
    return rows, as_of


def _num_or_none(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def store_spot(root: Path, rows: dict[str, dict[str, Any]], as_of: str) -> Path:
    target = root / SPOT_DIRECTORY / f"{as_of}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"as_of": as_of, "rows": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def load_spot(root: Path, limit: int = 2) -> list[dict[str, Any]]:
    """按日期倒序读最近 limit 份份额快照（坏文件跳过，不阻断分析）。"""
    directory = root / SPOT_DIRECTORY
    if not directory.is_dir():
        return []
    snapshots: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), reverse=True)[:limit]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload.get("rows"), dict):
            snapshots.append(payload)
    return snapshots


def share_delta(root: Path) -> dict[str, dict[str, Any]]:
    """每只 ETF 的最新份额与「较上一份快照」的变化。

    只有一份快照时 change 为 None——首次采集没有可比对象，如实标注
    「需要至少两次采集」，不编一个 0% 的假变化。
    """
    snapshots = load_spot(root)
    if not snapshots:
        return {}
    latest = snapshots[0]
    previous = snapshots[1] if len(snapshots) > 1 else None
    out: dict[str, dict[str, Any]] = {}
    for code, row in latest["rows"].items():
        share = row.get("share")
        change: float | None = None
        if previous and share and previous["rows"].get(code, {}).get("share"):
            base = previous["rows"][code]["share"]
            if base:
                change = share / base - 1
        out[code] = {
            "share": share,
            "mktcap": row.get("mktcap"),
            "share_change": change,
            "spot_as_of": latest["as_of"],
            "spot_prev_as_of": previous["as_of"] if previous else None,
        }
    return out


def refresh_spot(root: Path) -> dict[str, Any]:
    """联网采一份份额快照并留档；返回本次采到的行与日期。"""
    rows, as_of = fetch_spot()
    if not rows:
        raise ValueError("清单内 14 只 ETF 在现货行情里一只都没找到")
    store_spot(root, rows, as_of)
    return {"as_of": as_of, "n_rows": len(rows)}


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
    shares = share_delta(root)
    for bare, name in sorted(ETF_UNIVERSE.items()):
        frame = load(root, bare)
        if frame is None:
            missing.append(f"{bare} {name}")
            continue
        row = {"symbol": bare, "name": name, **_row(frame)}
        share = shares.get(bare)
        if share:
            row.update(
                {
                    "share": share["share"],
                    "mktcap": share["mktcap"],
                    "share_change": share["share_change"],
                    "spot_as_of": share["spot_as_of"],
                }
            )
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
        "spot_as_of": (shares and next(iter(shares.values()))["spot_as_of"]) or None,
        "note": "量比=当日额/前20日均额（不含当日）；水位=现价/近250日最高（含当日，今日新高即100%）；MA20偏离=现价/20日均-1；份额变化=最新份额/上一份快照-1（需要至少两次采集）",
    }
