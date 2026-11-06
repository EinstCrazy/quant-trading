"""自选股与分类：用户标注的集合，不是行情的缓存。

要守住的三条：代码归一化后才入库；删分类连引用一起摘，不留悬空标签；
API 层不存价格——涨跌每次从面板现读，面板不在就留空而不是编一个数。
"""

from __future__ import annotations

import pytest

# --- 存储层 -------------------------------------------------------------------


class TestStore:
    def test_add_normalises_the_symbol(self, tmp_path):
        from quant_research.watchlist import add_stock, load, path

        add_stock(tmp_path, "600519", categories=["白酒"])
        data = load(tmp_path)

        assert "600519.SH" in data["stocks"]
        assert data["stocks"]["600519.SH"]["categories"] == ["白酒"]
        assert path(tmp_path).is_file()

    def test_upsert_keeps_note_and_added_at(self, tmp_path):
        from quant_research.watchlist import add_stock, load

        add_stock(tmp_path, "600519.SH", categories=["白酒"])
        first = load(tmp_path)["stocks"]["600519.SH"]

        add_stock(
            tmp_path, "600519.SH", name="贵州茅台", categories=["白酒", "核心"], note="观察估值"
        )

        updated = load(tmp_path)["stocks"]["600519.SH"]
        assert updated["name"] == "贵州茅台"
        assert updated["note"] == "观察估值"
        assert updated["added_at"] == first["added_at"]
        assert updated["categories"] == ["白酒", "核心"]

    def test_removing_a_category_takes_the_references_with_it(self, tmp_path):
        """A deleted group must not linger as a tag on stocks it once held."""
        from quant_research.watchlist import add_stock, load, remove_category

        add_stock(tmp_path, "600519.SH", categories=["白酒", "核心"])
        add_stock(tmp_path, "000858.SZ", categories=["白酒"])

        data = remove_category(tmp_path, "白酒")

        assert data["categories"] == ["核心"]
        assert all("白酒" not in s["categories"] for s in data["stocks"].values())
        assert load(tmp_path)["stocks"]["600519.SH"]["categories"] == ["核心"]

    def test_a_corrupt_file_reads_as_empty_not_as_a_crash(self, tmp_path):
        from quant_research.watchlist import empty, load

        (tmp_path / "watchlist.json").write_text("{not json", encoding="utf-8")

        assert load(tmp_path) == empty()

    def test_remove_an_absent_stock_is_a_noop(self, tmp_path):
        from quant_research.watchlist import empty, remove_stock

        assert remove_stock(tmp_path, "600519.SH") == empty()


# --- API 层 -------------------------------------------------------------------


@pytest.fixture()
def wl_client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi", reason="web extra not installed")
    from fastapi.testclient import TestClient

    from quant_research.web import api as api_module
    from quant_research.web import create_app

    class _Collection:
        provider = "baostock"
        adjust = "qfq"
        output_directory = tmp_path / "raw"

    class _Data:
        directory = tmp_path / "data"

    class _Settings:
        collection = _Collection()
        data = _Data()

    settings = _Settings()
    monkeypatch.setattr(api_module, "load_settings", lambda *args, **kwargs: settings)
    return TestClient(create_app())


class TestAPI:
    def test_add_then_get_returns_the_stock(self, wl_client):
        added = wl_client.post("/api/watchlist", json={"symbol": "600519", "categories": ["白酒"]})
        assert added.status_code == 200

        payload = wl_client.get("/api/watchlist").json()
        assert [s["symbol"] for s in payload["stocks"]] == ["600519.SH"]
        assert payload["categories"] == ["白酒"]
        # 没有行情数据时不编造报价
        assert payload["stocks"][0]["last_close"] is None
        assert payload["stocks"][0]["change_pct"] is None

    def test_unknown_code_is_a_clear_400(self, wl_client):
        response = wl_client.post("/api/watchlist", json={"symbol": "999999.HZ"})
        assert response.status_code == 422

    def test_remove_stock(self, wl_client):
        wl_client.post("/api/watchlist", json={"symbol": "600519.SH"})

        payload = wl_client.delete("/api/watchlist/600519.SH").json()

        assert payload["stocks"] == []

    def test_category_lifecycle(self, wl_client):
        wl_client.post("/api/watchlist/categories", json={"name": "科技"})

        payload = wl_client.delete("/api/watchlist/categories/科技").json()

        assert payload["categories"] == []

    def test_blank_category_name_is_rejected(self, wl_client):
        assert wl_client.post("/api/watchlist/categories", json={"name": "  "}).status_code == 422
