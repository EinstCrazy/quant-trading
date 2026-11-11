"""批量导入：幂等、逐条报告——最怕的是「悄悄丢行」和「旧表格覆盖真实成本」。

自选导入不动已有条目（分类备注是用户手写的，批量粘贴最常见的事故
就是把它覆盖掉）；持仓导入默认跳过已有持仓（旧表格盖掉真实成本是
最贵的错误）。成功几条、重复几条、失败几条为什么失败，全都要摆明。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quant_research import holdings as holdings_store
from quant_research import watchlist as watchlist_store


class TestWatchlistImport:
    def test_parses_symbol_name_and_categories(self):
        from quant_research.web.api import _parse_watchlist_lines

        entries = _parse_watchlist_lines(
            "600519 贵州茅台 #白酒 #核心\n\n300750 宁德时代\nxx01 坏代码"
        )

        assert len(entries) == 3
        assert entries[0] == {
            "symbol": "600519",
            "name": "贵州茅台",
            "categories": ["白酒", "核心"],
        }
        assert entries[1]["name"] == "宁德时代" and entries[1]["categories"] == []
        assert entries[2]["symbol"] == "xx01"  # 无效代码交给 store 层报 invalid

    def test_batch_lands_in_one_write_with_names(self, tmp_path):
        result = watchlist_store.import_stocks(
            tmp_path,
            [
                {"symbol": "600519", "name": "贵州茅台", "categories": ["白酒"]},
                {"symbol": "300750"},
            ],
        )

        assert len(result["added"]) == 2
        data = watchlist_store.load(tmp_path)
        assert data["stocks"]["600519.SH"]["name"] == "贵州茅台"
        assert data["stocks"]["600519.SH"]["categories"] == ["白酒"]
        assert data["stocks"]["300750.SZ"]["categories"] == []
        assert "白酒" in data["categories"]

    def test_existing_entries_are_never_touched(self, tmp_path):
        """已在自选里的票不动：手写的分类备注不能被批量粘贴覆盖。"""
        watchlist_store.upsert_category(tmp_path, "白酒")
        watchlist_store.add_stock(
            tmp_path, "600519", name="旧名字", categories=["白酒"], note="手写备注"
        )

        result = watchlist_store.import_stocks(
            tmp_path,
            [{"symbol": "600519", "name": "批量新名字", "categories": ["其他"]}],
        )

        assert result["added"] == []
        assert result["duplicates"] == ["600519.SH"]
        data = watchlist_store.load(tmp_path)
        assert data["stocks"]["600519.SH"]["name"] == "旧名字"
        assert data["stocks"]["600519.SH"]["note"] == "手写备注"

    def test_invalid_codes_are_reported_per_line(self, tmp_path):
        result = watchlist_store.import_stocks(
            tmp_path,
            [
                {"symbol": ""},
                {"symbol": "not-a-code"},
                {"symbol": "600519"},
            ],
        )

        assert len(result["added"]) == 1
        assert [row["line"] for row in result["invalid"]] == ["1", "2"]
        assert all(
            "代码" in row["reason"] or "600519" not in row["reason"] for row in result["invalid"]
        )

    def test_duplicates_within_one_batch_land_once(self, tmp_path):
        result = watchlist_store.import_stocks(
            tmp_path,
            [{"symbol": "600519"}, {"symbol": "600519.SH"}],
        )

        assert len(result["added"]) == 1
        assert len(result["duplicates"]) == 1


class TestPositionImport:
    def test_batch_lands_with_shares_and_cost(self, tmp_path):
        result = holdings_store.import_positions(
            tmp_path,
            [
                {"symbol": "600519", "shares": "1000", "cost": "1500.5"},
                {"symbol": "000333", "shares": 20000, "cost": 55.2},
            ],
        )

        assert len(result["added"]) == 2
        data = holdings_store.load(tmp_path)
        assert data["positions"][holdings_store.entry_key("默认", "600519.SH")]["shares"] == 1000.0
        assert data["positions"][holdings_store.entry_key("默认", "600519.SH")]["cost"] == 1500.5

    def test_existing_positions_are_skipped_not_overwritten(self, tmp_path):
        """旧表格不能覆盖真实成本：默认跳过，想覆盖走单条 upsert。"""
        holdings_store.upsert(tmp_path, "600519", shares=500, cost=1900.0)

        result = holdings_store.import_positions(
            tmp_path, [{"symbol": "600519", "shares": 1000, "cost": 1500.5}]
        )

        assert result["duplicates"] == ["600519.SH（默认）"]
        assert result["added"] == []
        data = holdings_store.load(tmp_path)
        assert (
            data["positions"][holdings_store.entry_key("默认", "600519.SH")]["cost"] == 1900.0
        )  # 真实成本没被动

    def test_skip_existing_false_means_these_lines_are_the_truth(self, tmp_path):
        holdings_store.upsert(tmp_path, "600519", shares=500, cost=1900.0)

        result = holdings_store.import_positions(
            tmp_path,
            [{"symbol": "600519", "shares": 1000, "cost": 1500.5}],
            skip_existing=False,
        )

        assert len(result["added"]) == 1
        assert (
            holdings_store.load(tmp_path)["positions"][
                holdings_store.entry_key("默认", "600519.SH")
            ]["shares"]
            == 1000.0
        )

    def test_non_positive_or_unparsable_rows_are_reported(self, tmp_path):
        result = holdings_store.import_positions(
            tmp_path,
            [
                {"symbol": "600519", "shares": "0", "cost": "1500"},
                {"symbol": "000333", "shares": "abc", "cost": "55"},
                {"symbol": "600519", "shares": "1000", "cost": "1500.5"},
            ],
        )

        assert len(result["added"]) == 1
        assert [row["line"] for row in result["invalid"]] == ["1", "2"]


class TestImportAPI:
    @pytest.fixture()
    def imp_client(self, tmp_path, monkeypatch) -> TestClient:
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

    def test_watchlist_import_reports_every_outcome(self, imp_client):
        payload = imp_client.post(
            "/api/watchlist/import",
            json={"text": "600519 贵州茅台 #白酒\n600519 贵州茅台\nzzzz9 坏的"},
        ).json()

        assert payload["n_added"] == 1
        assert len(payload["duplicates"]) == 1
        assert len(payload["invalid"]) == 1
        assert payload["added"][0]["name"] == "贵州茅台"

    def test_portfolio_import_skips_existing_by_default(self, imp_client, tmp_path):
        holdings_store.upsert(tmp_path, "600519", shares=500, cost=1900.0)

        payload = imp_client.post(
            "/api/portfolio/import",
            json={"text": "600519 1000 1500.5\n000333 20000 55.2\n600519 \n"},
        ).json()

        assert payload["n_added"] == 1  # 只有 000333 进来
        assert payload["duplicates"] == ["600519.SH（默认）"]
        assert (
            payload["invalid"][0]["reason"].endswith("float() argument")
            or "600519" in payload["invalid"][0]["reason"]
        )
