"""Excel 批量导入：表头宽容映射，认不出来如实报错并列出实际表头。

用户交来的表格表头五花八门（数量/持股数/买入价/成本单价…），能对上
的都对上；对不上的绝不猜——猜错列名会把成本填进股数，那比拒绝导入
贵得多。空行跳过、行号从 2 起（Excel 的 1 是表头），报告用 Excel 行号
让用户回到自己的表格里能直接找到那行。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from quant_research import holdings as holdings_store
from quant_research import watchlist as watchlist_store


def _xlsx(rows: list[list[object]], header: list[str]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestWatchlistExcel:
    @pytest.fixture()
    def xl_client(self, tmp_path, monkeypatch) -> TestClient:
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

            class collection:
                provider = "baostock"
                adjust = "qfq"

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        client = TestClient(create_app())
        client._tmp = tmp_path
        return client

    def _upload(self, client: TestClient, path: str, content: bytes) -> object:
        return client.post(
            path,
            files={
                "file": (
                    "list.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        ).json()

    def test_imports_symbols_names_and_split_categories(self, xl_client):
        payload = self._upload(
            xl_client,
            "/api/watchlist/import/excel",
            _xlsx(
                [["600519", "贵州茅台", "白酒、核心"], ["300750", "宁德时代", ""]],
                ["证券代码", "名称", "分类"],
            ),
        )

        assert payload["n_added"] == 2
        data = watchlist_store.load(xl_client._tmp)
        assert data["stocks"]["600519.SH"]["categories"] == ["白酒", "核心"]

    def test_unknown_headers_are_refused_not_guessed(self, xl_client):
        """认不出代码列：如实报错并列出实际表头，不猜哪列是代码。"""
        payload = self._upload(
            xl_client,
            "/api/watchlist/import/excel",
            _xlsx([["600519", "贵州茅台"]], ["ticker", "股票名"]),
        )

        assert "detail" in payload
        assert "ticker" in payload["detail"]

    def test_blank_rows_are_skipped_with_excel_line_numbers(self, xl_client):
        payload = self._upload(
            xl_client,
            "/api/watchlist/import/excel",
            _xlsx(
                [["600519", "贵州茅台", ""], [None, None, None], ["zzzz9", "坏代码", ""]],
                ["代码", "名称", "分类"],
            ),
        )

        assert payload["n_added"] == 1
        # 第 3 行整行空白跳过；第 4 行坏代码带 Excel 行号报告
        assert any(row["line"] == "4" for row in payload["invalid"])

    def test_broken_file_is_refused_with_a_reason(self, xl_client):
        payload = self._upload(
            xl_client,
            "/api/watchlist/import/excel",
            b"not an xlsx at all",
        )

        assert "detail" in payload


class TestPortfolioExcel:
    @pytest.fixture()
    def pf_client(self, tmp_path, monkeypatch) -> TestClient:
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

            class collection:
                provider = "baostock"
                adjust = "qfq"

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        client = TestClient(create_app())
        client._tmp = tmp_path
        return client

    def _upload(self, client: TestClient, path: str, content: bytes) -> object:
        return client.post(
            path,
            files={
                "file": (
                    "book.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        ).json()

    def test_alias_headers_land_shares_and_cost(self, pf_client, tmp_path):
        """「数量」「买入价」这类写法能对上——对上的列名宽容映射。"""
        payload = self._upload(
            pf_client,
            "/api/portfolio/import/excel",
            _xlsx(
                [["600519", 1000, 1500.5]],
                ["证券代码", "数量", "买入价"],
            ),
        )

        assert payload["n_added"] == 1
        data = holdings_store.load(tmp_path)
        assert data["positions"][holdings_store.entry_key("默认", "600519.SH")][
            "cost"
        ] == pytest.approx(1500.5)

    def test_missing_cost_column_is_refused(self, pf_client):
        """缺成本列：拒绝并列出实际表头——把成本填进股数比拒绝贵得多。"""
        payload = self._upload(
            pf_client,
            "/api/portfolio/import/excel",
            _xlsx([["600519", 1000]], ["代码", "股数"]),
        )

        assert "detail" in payload
        assert "成本" in payload["detail"]


class TestExcelFormatSpec:
    """格式说明必须等于实现：别名从 _EXCEL_HEADER_ALIASES 出，说明不会
    在改了别名之后悄悄变成骗人的文档。"""

    @pytest.fixture()
    def spec(self) -> dict:
        from fastapi.testclient import TestClient

        from quant_research.web import create_app

        return TestClient(create_app()).get("/api/import/excel/spec").json()

    def test_aliases_come_from_the_parser(self, spec):
        from quant_research.web.api import _EXCEL_HEADER_ALIASES

        assert spec["aliases"] == {
            field: list(aliases) for field, aliases in _EXCEL_HEADER_ALIASES.items()
        }

    def test_required_columns_differ_by_target(self, spec):
        """持仓要三列，自选只要代码——说明写错了比没有说明更糟。"""
        assert spec["required"]["portfolio"] == ["symbol", "shares", "cost"]
        assert spec["required"]["watchlist"] == ["symbol"]

    def test_rules_cover_the_traps_users_hit(self, spec):
        """每条规则都对应一个真实踩过的坑：表头行、空行、xlsx、公式缓存。"""
        text = "".join(spec["rules"])
        for keyword in ("第 1 行", "空行", "xlsx", "公式"):
            assert keyword in text

    def test_examples_match_their_required_columns(self, spec):
        """示例表头必须覆盖必填列——否则照抄示例反而导不进来。"""
        from quant_research.web.api import _EXCEL_HEADER_ALIASES

        for kind, example in spec["examples"].items():
            header = [str(h) for h in example[0]]
            for field in spec["required"][kind]:
                assert any(alias in header for alias in _EXCEL_HEADER_ALIASES[field]), (kind, field)

    def test_documented_example_actually_imports(self, tmp_path, monkeypatch):
        """照抄说明里的示例表，必须真的导得进来——说明与解析器的最终一致性。"""
        import io

        from fastapi.testclient import TestClient
        from openpyxl import Workbook

        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        client = TestClient(create_app())
        spec = client.get("/api/import/excel/spec").json()

        for kind, endpoint in (("portfolio", "/api/portfolio"), ("watchlist", "/api/watchlist")):
            workbook = Workbook()
            sheet = workbook.active
            for row in spec["examples"][kind]:
                sheet.append(list(row))
            buffer = io.BytesIO()
            workbook.save(buffer)
            payload = client.post(
                f"{endpoint}/import/excel",
                files={
                    "file": (
                        f"{kind}.xlsx",
                        buffer.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            ).json()
            assert payload["n_added"] == 2, (kind, payload)
            assert not payload["invalid"], (kind, payload["invalid"])
