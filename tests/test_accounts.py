"""多账户持仓：同一只票在不同券商各记一条，互不覆盖。

账户只是分组标签：本账本回答「这只票在哪个户里、以什么成本拿了多少」，
券商现金余额与资金划转不在这里。旧文件（键=代码）读入时自动进「默认」
账户——升级不能丢用户已有的账。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research import holdings
from quant_research.data.loader import PricePanel


class TestStore:
    def test_same_symbol_lands_in_two_accounts_independently(self, tmp_path):
        """同一票两个账户 = 两条独立持仓，不能让后录的盖掉先录的。"""
        holdings.upsert(tmp_path, "600519", shares=100, cost=1500, account="华泰")
        holdings.upsert(tmp_path, "600519", shares=200, cost=1600, account="中信")

        data = holdings.load(tmp_path)

        assert sorted(data["positions"]) == sorted(
            [holdings.entry_key("华泰", "600519.SH"), holdings.entry_key("中信", "600519.SH")]
        )
        assert holdings.entry_key("中信", "600519.SH") in data["positions"]
        assert data["positions"][holdings.entry_key("华泰", "600519.SH")]["shares"] == 100
        assert data["positions"][holdings.entry_key("中信", "600519.SH")]["cost"] == 1600

    def test_symbols_dedupe_the_same_stock(self, tmp_path):
        """持仓 ∪ 自选要用代码集合：同一票两账户只算一只。"""
        holdings.upsert(tmp_path, "600519", shares=100, cost=1500, account="华泰")
        holdings.upsert(tmp_path, "600519", shares=200, cost=1600, account="中信")

        assert holdings.symbols(holdings.load(tmp_path)) == ["600519.SH"]

    def test_legacy_file_migrates_to_default_account(self, tmp_path):
        """旧账本（键就是代码、无 account）读进来就是默认账户——不丢账。"""
        legacy = {
            "positions": {
                "600519.SH": {
                    "shares": 100,
                    "cost": 1500.0,
                    "note": "旧记录",
                    "added_at": "2026-11-06T10:00:00",
                }
            }
        }
        (tmp_path / "holdings.json").write_text(json.dumps(legacy), encoding="utf-8")

        data = holdings.load(tmp_path)
        entry = data["positions"][holdings.entry_key("默认", "600519.SH")]

        assert entry["shares"] == 100 and entry["note"] == "旧记录"
        assert holdings.accounts(data) == ["默认"]

    def test_remove_needs_account_when_two_accounts_hold_it(self, tmp_path):
        """一次点击删掉两个账户的持仓是最贵的误操作：要求指定账户。"""
        holdings.upsert(tmp_path, "600519", shares=100, cost=1500, account="华泰")
        holdings.upsert(tmp_path, "600519", shares=200, cost=1600, account="中信")

        with pytest.raises(ValueError, match="指定账户"):
            holdings.remove(tmp_path, "600519")

        holdings.remove(tmp_path, "600519", account="华泰")
        data = holdings.load(tmp_path)
        assert holdings.entry_key("华泰", "600519.SH") not in data["positions"]
        assert holdings.entry_key("中信", "600519.SH") in data["positions"]

    def test_single_account_still_deletes_without_asking(self, tmp_path):
        holdings.upsert(tmp_path, "600519", shares=100, cost=1500)

        data = holdings.remove(tmp_path, "600519")

        assert data["positions"] == {}

    def test_account_name_validation(self, tmp_path):
        with pytest.raises(ValueError, match="分隔符"):
            holdings.upsert(tmp_path, "600519", shares=1, cost=1, account="华泰::主户")

    def test_blank_account_falls_back_to_default(self, tmp_path):
        holdings.upsert(tmp_path, "600519", shares=100, cost=1500, account="  ")

        assert holdings.accounts(holdings.load(tmp_path)) == ["默认"]

    def test_import_brings_account_column(self, tmp_path):
        result = holdings.import_positions(
            tmp_path,
            [
                {"symbol": "600519", "shares": 100, "cost": 1500, "account": "华泰"},
                {"symbol": "600519", "shares": 200, "cost": 1600, "account": "中信"},
                {"symbol": "000333", "shares": 100, "cost": 55},
            ],
        )

        assert len(result["added"]) == 3
        assert not result["invalid"]
        assert holdings.accounts(holdings.load(tmp_path)) == ["默认", "中信", "华泰"]


class TestPortfolioAPI:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        index = pd.bdate_range("2024-01-01", periods=30)
        panel = PricePanel(
            prices=pd.DataFrame({"600519.SH": 1500.0, "000333.SZ": 60.0}, index=index)
        )

        class _Settings:
            class data:
                directory = tmp_path

            class collection:
                provider = "akshare"
                adjust = "qfq"

            indicators = None

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: panel)
        return TestClient(create_app())

    def test_payload_carries_accounts_and_per_account_totals(self, client):
        client.post(
            "/api/portfolio",
            json={"symbol": "600519.SH", "shares": 100, "cost": 1400, "account": "华泰"},
        )
        client.post(
            "/api/portfolio",
            json={"symbol": "600519.SH", "shares": 200, "cost": 1600, "account": "中信"},
        )
        client.post("/api/portfolio", json={"symbol": "000333.SZ", "shares": 1000, "cost": 50})

        payload = client.get("/api/portfolio").json()

        assert payload["account_names"] == ["默认", "中信", "华泰"]
        assert len(payload["positions"]) == 3  # 同一票两账户是两行
        by_account = {row["account"]: row for row in payload["accounts"]}
        assert by_account["华泰"]["n_positions"] == 1
        assert by_account["华泰"]["market_value"] == pytest.approx(150000.0)
        assert by_account["华泰"]["pnl"] == pytest.approx(10000.0)
        assert by_account["默认"]["pnl"] == pytest.approx(10000.0)

    def test_delete_requires_account_when_ambiguous(self, client):
        client.post(
            "/api/portfolio",
            json={"symbol": "600519.SH", "shares": 100, "cost": 1400, "account": "华泰"},
        )
        client.post(
            "/api/portfolio",
            json={"symbol": "600519.SH", "shares": 200, "cost": 1600, "account": "中信"},
        )

        response = client.delete("/api/portfolio/600519.SH")

        assert response.status_code == 400 and "指定账户" in response.json()["detail"]

        ok = client.delete("/api/portfolio/600519.SH", params={"account": "华泰"})

        assert ok.status_code == 200
        assert len(ok.json()["positions"]) == 1

    def test_empty_account_name_goes_to_default(self, client):
        client.post(
            "/api/portfolio",
            json={"symbol": "600519.SH", "shares": 100, "cost": 1400, "account": ""},
        )

        payload = client.get("/api/portfolio").json()

        assert payload["account_names"] == ["默认"]

    def test_unpriced_row_keeps_account_pnl_pct_blank(self, client, monkeypatch):
        """账户里有票缺现价：市值只含部分票，比率会失真——不给比率的假精确。"""
        index = pd.bdate_range("2024-01-01", periods=30)
        monkeypatch.setattr(
            "quant_research.web.api._load_panel",
            lambda *a, **k: PricePanel(prices=pd.DataFrame({"600519.SH": 1500.0}, index=index)),
        )
        client.post(
            "/api/portfolio",
            json={"symbol": "600519.SH", "shares": 100, "cost": 1400, "account": "华泰"},
        )
        client.post(
            "/api/portfolio",
            json={"symbol": "600000.SH", "shares": 100, "cost": 10, "account": "华泰"},
        )

        payload = client.get("/api/portfolio").json()
        bucket = payload["accounts"][0]

        assert bucket["n_unpriced"] == 1
        assert bucket["pnl_pct"] is None

    def test_excel_account_column_lands_two_accounts(self, client):
        """Excel 带「账户」列：同一票两账户各一行，一次导入两条。"""
        import io

        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["代码", "股数", "成本价", "账户"])
        sheet.append(["600519", 100, 1400, "华泰"])
        sheet.append(["600519", 200, 1600, "中信"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        payload = client.post(
            "/api/portfolio/import/excel",
            files={
                "file": (
                    "book.xlsx",
                    buffer.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        ).json()

        assert payload["n_added"] == 2
        assert sorted(row["account"] for row in payload["added"]) == ["中信", "华泰"]
