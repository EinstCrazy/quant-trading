"""历史复盘与用户备注：存档要能翻回去看，备注要不被覆盖。

守住三条：备注与系统结论分开存（「机器说的」和「人写的」不混字段）；
省略备注的重存继承旧备注（重复存档不该抹掉手写注解，显式空串才是
清除）；历史只读目录不联网——存档存的是「那天看到的」，不是行情。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quant_research import holdings as store
from quant_research import review_archive as archive


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """复盘要走面板（raw + cache）：把真实数据树最小集拷进隔离目录。"""
    import shutil

    from quant_research.config import load_settings

    real = load_settings()
    src = real.data.directory
    for sub in ("raw", "cache", "processed"):
        shutil.copytree(src / sub, tmp_path / sub, dirs_exist_ok=True)

    store.upsert(tmp_path, "600519.SH", shares=100, cost=1500, account="华泰")
    store.upsert(tmp_path, "000559.SZ", shares=2000, cost=12, account="华泰")

    from quant_research.web import api as api_module
    from quant_research.web import create_app

    class _Settings:
        class data:
            directory = tmp_path

        collection = real.collection
        review = real.review

    monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
    return TestClient(create_app())


def _save(client: TestClient, note: str | None = None):
    body = {"note": note} if note is not None else None
    return client.post("/api/portfolio/review/save", json=body)


class TestSaveWithNote:
    def test_note_travels_into_the_archive(self, client, tmp_path):
        payload = _save(client, "600519 位置不对，明天减半").json()

        assert payload["date"]
        saved = json.loads(
            (tmp_path / "reviews" / f"{payload['date']}.json").read_text(encoding="utf-8")
        )
        assert saved["user_note"] == "600519 位置不对，明天减半"
        # 与系统结论分字段——「机器说的」和「人写的」不混
        assert "summary_note" in saved

    def test_resave_without_note_keeps_the_old_one(self, client):
        """重复存档（忘填备注）不能抹掉手写注解——省略即继承。"""
        first = _save(client, "先记一笔：明天盯止损").json()
        second = _save(client).json()

        assert second["date"] == first["date"]
        detail = client.get(f"/api/portfolio/review/archive/{first['date']}").json()
        assert detail["user_note"] == "先记一笔：明天盯止损"

    def test_empty_string_clears_the_note(self, client):
        """显式空串才是清除——用户主动删备注要能删得掉。"""
        day = _save(client, "作废这条").json()["date"]
        _save(client, "")

        detail = client.get(f"/api/portfolio/review/archive/{day}").json()
        assert detail["user_note"] is None

    def test_note_over_500_chars_is_rejected(self, client):
        response = client.post("/api/portfolio/review/save", json={"note": "长" * 501})

        assert response.status_code == 422


class TestHistory:
    def test_lists_archives_newest_first(self, client, tmp_path):
        archive.save(
            tmp_path,
            {"as_of": "2026-11-10", "provider": "baostock", "summary": {}, "positions": []},
        )
        _save(client, "今天写一条")

        history = client.get("/api/portfolio/review/history").json()["history"]

        dates = [h["date"] for h in history]
        assert dates[0] > dates[-1] if len(dates) > 1 else len(dates) == 2
        today = next(h for h in history if h["date"] not in ("2026-11-10",))
        assert today["user_note"] == "今天写一条"
        assert today["n_positions"] == 2

    def test_empty_directory_is_an_empty_list(self, tmp_path, monkeypatch):
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

            collection = api_module.load_settings().collection
            review = api_module.load_settings().review

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        payload = TestClient(create_app()).get("/api/portfolio/review/history").json()

        assert payload == {"history": []}

    def test_archive_dates_sort_newest_first_in_history(self, client, tmp_path):
        import json as json_mod

        folder = tmp_path / "reviews"
        folder.mkdir(exist_ok=True)
        for date in ("2026-01-01", "2026-06-01", "2026-03-01"):
            (folder / f"{date}.json").write_text(
                json_mod.dumps(
                    {
                        "date": date,
                        "positions": {
                            "华泰::600519.SH": {
                                "symbol": "600519.SH",
                                "account": "华泰",
                                "stance": "hold",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        dates = [h["date"] for h in client.get("/api/portfolio/review/history").json()["history"]]

        assert dates == sorted(dates, reverse=True)


class TestArchiveDetail:
    def test_detail_has_rows_note_and_changes(self, client, tmp_path):
        day = _save(client, "备注一条").json()["date"]
        archive.save(
            tmp_path,
            {
                "as_of": "2020-01-01",
                "provider": "baostock",
                "summary": {},
                "positions": [{"symbol": "600519.SH", "account": "华泰", "stance": "trim"}],
            },
        )

        detail = client.get(f"/api/portfolio/review/archive/{day}").json()

        assert detail["user_note"] == "备注一条"
        assert any(p["symbol"] == "600519.SH" for p in detail["positions"])
        # 与再上一次（2020-01-01）比：600519 从 trim 变 hold
        assert any(c["symbol"] == "600519.SH" and c["from"] == "trim" for c in detail["changes"])

    def test_missing_date_is_404_with_direction(self, client):
        response = client.get("/api/portfolio/review/archive/2000-01-01")

        assert response.status_code == 404
        assert "存为今日复盘" in response.json()["detail"]
