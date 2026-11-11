"""多指数对比：主口径不变（上证做大盘量能），其余是结构对照。

加指数的坑：`_market_volume` 靠 `next(iter(INDEXES))` 取主口径——
依赖 dict 顺序，上证必须永远排第一，否则大盘成交额口径悄悄漂移。
没采过的指数进 missing 而不是静默少行：对比表里少一行会被读成
「那个指数没涨没跌」。
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research.data import index as index_mod


def _frame(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": closes, "amount": [1e11] * len(closes)},
        index=pd.bdate_range("2026-11-01", periods=len(closes)),
    )


class TestIndexes:
    def test_first_index_is_still_the_market_volume_baseline(self):
        """主口径回归：大盘量能必须始终用上证——加指数不许让它漂移。"""
        assert next(iter(index_mod.INDEXES)) == "sh000001"

    def test_compare_reads_every_cached_index(self, tmp_path):
        index_mod.store(tmp_path, "sh000001", _frame([3000.0, 3015.0]))
        index_mod.store(tmp_path, "sh000300", _frame([4000.0, 4012.0]))

        payload = index_mod.compare(tmp_path)

        assert payload["available"] is True
        assert payload["rows"][0]["symbol"] == "sh000001"
        assert payload["rows"][0]["day_change"] == pytest.approx(0.005)
        assert payload["rows"][1]["name"] == "沪深300"

    def test_uncached_indexes_are_missing_not_silent(self, tmp_path):
        """没采过的指数进 missing：少一行在对比表里会被读成「没涨没跌」。"""
        index_mod.store(tmp_path, "sh000001", _frame([3000.0, 3015.0]))

        payload = index_mod.compare(tmp_path)

        assert len(payload["rows"]) == 1
        assert any("创业板指" in entry for entry in payload["missing"])

    def test_short_history_reports_null_change(self, tmp_path):
        index_mod.store(tmp_path, "sh000001", _frame([3000.0]))

        row = index_mod.compare(tmp_path)["rows"][0]

        assert row["day_change"] is None  # 一天数据没有「涨跌」可算


class TestIndexesAPI:
    @pytest.fixture()
    def ix_client(self, tmp_path, monkeypatch) -> TestClient:
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

            class collection:
                provider = "baostock"
                adjust = "qfq"

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        return TestClient(create_app())

    def test_indexes_endpoint_is_read_only(self, ix_client, tmp_path):
        payload = ix_client.get("/api/indexes").json()

        assert payload["available"] is False
        assert len(payload["missing"]) == len(index_mod.INDEXES)
        assert "主口径" in payload["note"]

    def test_indexes_endpoint_after_a_store(self, ix_client, tmp_path):
        index_mod.store(tmp_path, "sh000001", _frame([3000.0, 3015.0]))

        payload = ix_client.get("/api/indexes").json()

        assert payload["available"] is True
        assert payload["rows"][0]["last"] == pytest.approx(3015.0)
