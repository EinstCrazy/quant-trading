"""扩样本：从行业归属表按行业等量挑票，与现有样本合并。

挑选标准刻意做成「透明、可复现，但承认任意」：
- 每个证监会行业取代码序前 3 只——没有市值/流动性数据可用，代码序是唯一
  稳定的排序键；任意但要诚实，选出来的清单落在磁盘上可检查；
- 排除 ST/*ST（风险样本会污染分化度统计）、排除北交所（baostock 日线覆盖
  不完整）；
- 现有 30 只无条件保留——它们是各视图历史统计的延续。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_research.config import load_settings
from quant_research.data import universe

PER_SECTOR = 3


def pick(settings_dir: Path) -> tuple[list[str], list[str]]:
    names = universe.load(settings_dir)
    name_of = dict(zip(names["symbol"], names["name"])) if not names.empty else {}
    sectors = universe.load_sectors(settings_dir)

    # 现有样本：无条件保留
    raw = settings_dir / "raw" / "baostock" / "daily"
    current = sorted(p.stem for p in raw.glob("*.parquet")) if raw.is_dir() else []

    candidates = sectors.copy()
    candidates = candidates[~candidates["symbol"].str.startswith(("8", "4", "9"))]  # 北交所/退市
    candidates["name"] = candidates["symbol"].map(name_of).fillna("")
    candidates = candidates[~candidates["name"].str.contains("ST", case=False, na=False)]
    candidates = candidates.sort_values("symbol").groupby("sector", sort=False).head(PER_SECTOR)

    picked = sorted(set(candidates["symbol"]) | set(current))
    return picked, current


if __name__ == "__main__":
    settings = load_settings()
    picked, current = pick(settings.data.directory)
    print(f"现有 {len(current)} 只；每行业 {PER_SECTOR} 只后合计 {len(picked)} 只")
    out = settings.data.directory / "universe" / "sample_targets.txt"
    out.write_text("\n".join(picked), encoding="utf-8")
    print("清单已写入", out)
