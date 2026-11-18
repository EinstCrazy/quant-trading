"""选股名单带依据：分数是结论，分量是理由。

只有分数的名单无法回溯「那天为什么选它」；收盘随行，免得买多少钱
还得再查面板。ledger 存 picks_detail 快照，旧记录向后兼容。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from quant_research.data.loader import PricePanel
from quant_research.scoring.screen import screen

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


def _panel() -> PricePanel:
    index = pd.bdate_range("2026-06-01", periods=70)
    symbols = ["600000.SH", "000001.SZ", "600519.SH"]
    base = pd.DataFrame({sym: 10.0 + i for i, sym in enumerate(symbols)}, index=index)
    drift = pd.DataFrame(
        {
            sym: pd.Series(range(70), index=index) * (0.02 - 0.01 * i)
            for i, sym in enumerate(symbols)
        },
        index=index,
    )
    prices = (base + drift).astype("float64")
    return PricePanel(
        prices=prices,
        extra={
            "open": prices.copy(),
            "high": prices.copy(),
            "low": prices.copy(),
            "amount": prices.copy() * 1e6,
        },
    )


class TestScreenPickDetail:
    def test_picks_frame_carries_close_and_factors(self):
        """picks 帧带收盘与各因子分量列。"""
        result = screen(_panel(), top_n=2, min_history=60)
        assert "close" in result.picks.columns
        assert "momentum" in result.picks.columns
        assert "turnover" in result.picks.columns
        assert result.picks["close"].notna().all()

    def test_ledger_keeps_detail_snapshot(self, tmp_path):
        """ledger 存 picks_detail：每只票收盘/分量/成交额齐全。"""
        from quant_research.scoring.ledger import ScreenLedger

        result = screen(_panel(), top_n=2, min_history=60)
        ledger = ScreenLedger(tmp_path / "ledger.jsonl")
        entry = ledger.record(result, provider="baostock", adjust="qfq")
        symbol = entry.symbols[0]
        detail = entry.picks_detail[symbol]
        assert "close" in detail
        assert "momentum" in detail
        assert "turnover" in detail
        assert detail["close"] > 0
        reread = ledger.get(entry.as_of)
        assert reread is not None
        assert "close" in reread.picks_detail[symbol]

    def test_legacy_record_without_detail_still_reads(self, tmp_path):
        """旧 ledger 行（无 picks_detail 字段）读得回来——不炸历史。"""
        from quant_research.scoring.ledger import ScreenLedger

        path = tmp_path / "ledger.jsonl"
        line = {
            "as_of": "2026-11-01",
            "created_at": "2026-11-01T18:30:00",
            "provider": "akshare",
            "adjust": "qfq",
            "picks": {"600000.SH": 1.23},
            "universe": 50,
            "dropped": {},
            "params": {},
            "note": "",
        }
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        ledger = ScreenLedger(path)
        record = ledger.get("2026-11-01")
        assert record is not None
        assert record.picks_detail == {}
        assert record.picks == {"600000.SH": 1.23}
