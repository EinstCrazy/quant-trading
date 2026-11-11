"""账户增删改查：账本层原子操作 + 复盘存档键迁移 + API 端到端。

守住三条：编辑不靠「删了重加」拼（added_at 不丢）、重命名遇重叠持仓
拒绝（合并会把「哪个账户该卖多少」弄丢）、存档键跟着账本改名走（键对
不上，「和上次比什么变了」就永远比不上）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quant_research import holdings as store
from quant_research import review_archive as archive


@pytest.fixture()
def book(tmp_path: Path) -> Path:
    store.upsert(tmp_path, "600519.SH", shares=100, cost=1500, account="华泰")
    store.upsert(tmp_path, "000559.SZ", shares=2000, cost=12, account="华泰")
    store.upsert(tmp_path, "600519.SH", shares=50, cost=1600, account="中信")
    return tmp_path


class TestUpdate:
    def test_edit_keeps_added_at(self, book):
        """改股数不能重置买入时间——「删了重加」会把加仓记成建仓。"""
        before = store.load(book)["positions"]["华泰::600519.SH"]["added_at"]

        store.update(book, "600519.SH", account="华泰", shares=120, note="加仓")

        entry = store.load(book)["positions"]["华泰::600519.SH"]
        assert entry["shares"] == 120
        assert entry["note"] == "加仓"
        assert entry["added_at"] == before

    def test_move_lot_to_another_account(self, book):
        store.update(book, "000559.SZ", account="华泰", to_account="中信")

        keys = set(store.load(book)["positions"])
        assert "中信::000559.SZ" in keys
        assert "华泰::000559.SZ" not in keys

    def test_move_refuses_when_the_target_already_holds_it(self, book):
        with pytest.raises(ValueError, match="已有持仓"):
            store.update(book, "600519.SH", account="华泰", to_account="中信")

    def test_ambiguous_symbol_requires_an_account(self, book):
        with pytest.raises(ValueError, match="指定账户"):
            store.update(book, "600519.SH", shares=10)


class TestRenameAccount:
    def test_every_lot_moves_and_added_at_survives(self, book):
        store.rename_account(book, "华泰", "国泰")

        keys = set(store.load(book)["positions"])
        assert {"国泰::600519.SH", "国泰::000559.SZ", "中信::600519.SH"} == keys
        assert store.load(book)["positions"]["国泰::600519.SH"]["added_at"]

    def test_overlap_refuses_the_merge(self, book):
        """两边都有 600519：合并会弄丢「哪个账户该卖多少」，如实拒绝。"""
        with pytest.raises(ValueError, match="600519"):
            store.rename_account(book, "华泰", "中信")

    def test_unknown_account_is_400_material(self, book):
        with pytest.raises(ValueError, match="不存在"):
            store.rename_account(book, "不存在", "别的")


class TestRemoveAccount:
    def test_drops_every_lot_in_the_account(self, book):
        store.remove_account(book, "华泰")

        assert set(store.load(book)["positions"]) == {"中信::600519.SH"}

    def test_unknown_account_is_400_material(self, book):
        with pytest.raises(ValueError, match="不存在"):
            store.remove_account(book, "不存在")


class TestArchiveMigration:
    def test_rename_moves_every_archive_key(self, tmp_path):
        """存档键带账户：账本改名存档不改，对比就永远对不上。"""
        archive.save(
            tmp_path,
            {
                "as_of": "2026-11-11",
                "provider": "baostock",
                "summary": {},
                "positions": [{"symbol": "600519.SH", "account": "华泰", "stance": "hold"}],
            },
        )
        archive.save(
            tmp_path,
            {
                "as_of": "2026-11-10",
                "provider": "baostock",
                "summary": {},
                "positions": [{"symbol": "600519.SH", "account": "中信", "stance": "trim"}],
            },
        )

        changed = archive.rename_account(tmp_path, "华泰", "国泰")

        assert changed == 1  # 只有华泰那份动过
        renamed = json.loads((tmp_path / "reviews" / "2026-11-11.json").read_text(encoding="utf-8"))
        assert "国泰::600519.SH" in renamed["positions"]
        assert renamed["positions"]["国泰::600519.SH"]["account"] == "国泰"
        untouched = json.loads(
            (tmp_path / "reviews" / "2026-11-10.json").read_text(encoding="utf-8")
        )
        assert "中信::600519.SH" in untouched["positions"]

    def test_dropping_an_account_keeps_history(self, tmp_path):
        """删账户不动存档：那是「那天我看到的结论」，不是持仓的附属。"""
        archive.save(
            tmp_path,
            {
                "as_of": "2026-11-11",
                "provider": "baostock",
                "summary": {},
                "positions": [{"symbol": "600519.SH", "account": "华泰", "stance": "hold"}],
            },
        )

        store.upsert(tmp_path, "600519.SH", shares=100, cost=1500, account="华泰")
        store.remove_account(tmp_path, "华泰")

        assert (tmp_path / "reviews" / "2026-11-11.json").is_file()


class TestAccountAPI:
    """端到端：PATCH 编辑 / 挪账户、重命名同步存档、删除账户。"""

    @pytest.fixture()
    def client(self, book, monkeypatch):
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = book

            class collection:
                provider = "baostock"
                adjust = "qfq"
                provider_options: dict = {}  # noqa: RUF012

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        return TestClient(create_app())

    def test_patch_edits_one_lot(self, client):
        payload = client.patch(
            "/api/portfolio/600519.SH",
            json={"shares": 130, "note": "加仓"},
            params={"account": "华泰"},
        ).json()

        row = next(
            p for p in payload["positions"] if p["account"] == "华泰" and p["symbol"] == "600519.SH"
        )
        assert row["shares"] == 130
        assert row["note"] == "加仓"

    def test_patch_move_conflict_is_400(self, client):
        response = client.patch(
            "/api/portfolio/600519.SH", json={"to_account": "中信"}, params={"account": "华泰"}
        )

        assert response.status_code == 400
        assert "已有持仓" in response.json()["detail"]

    def test_rename_syncs_the_archive(self, client):
        archive.save(
            _root(client),
            {
                "as_of": "2026-11-11",
                "provider": "baostock",
                "summary": {},
                "positions": [{"symbol": "600519.SH", "account": "华泰", "stance": "hold"}],
            },
        )

        payload = client.post(
            "/api/portfolio/accounts/rename", json={"old": "华泰", "new": "国泰"}
        ).json()

        assert set(payload["account_names"]) == {"国泰", "中信"}
        saved = json.loads(
            (_root(client) / "reviews" / "2026-11-11.json").read_text(encoding="utf-8")
        )
        assert "国泰::600519.SH" in saved["positions"]

    def test_drop_account_removes_every_lot(self, client):
        payload = client.delete("/api/portfolio/accounts/华泰").json()

        assert {f"{p['account']}::{p['symbol']}" for p in payload["positions"]} == {
            "中信::600519.SH"
        }


def _root(client: TestClient) -> Path:
    """数据目录：load_settings 被 fixture monkeypatch 到 book 目录。"""
    from quant_research.web import api as api_module

    return Path(str(api_module.load_settings().data.directory))
