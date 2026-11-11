"""战法：清单结构、今日环境标注规则、API 的「缺数据照给战法」约定。

守住的几条：不出现胜率数字（没回测过就不假装有）；「今日顺/慎/避」由
量价四象限与外围情绪两个客观状态推出，规则可读可测；环境缺一块就少一块
标注，战法本体照给——「数据不足」要和「无事可做」分开说。
"""

from __future__ import annotations

import re

import numpy as np
import pytest
from fastapi.testclient import TestClient

from quant_research.playbook import PLAYS, analyze

SUNNY = {"quadrant": "放量上涨", "ratio": 1.4, "level": "放量", "streak": 2}
GLOOMY = {"quadrant": "放量下跌", "ratio": 1.5, "level": "放量", "streak": 0}
DRY = {"quadrant": "缩量下跌", "ratio": 0.45, "level": "地量", "streak": 0}
BALANCED = {"quadrant": "平量上涨", "ratio": 1.0, "level": "平量", "streak": 0}


def _by_key(payload: dict, key: str) -> dict:
    return next(play for play in payload["plays"] if play["key"] == key)


class TestCatalog:
    def test_the_first_play_is_one_red_rules_all(self):
        assert PLAYS[0].key == "first-red"
        assert "一红" in PLAYS[0].name

    def test_every_play_has_all_four_sections_filled(self):
        for play in PLAYS:
            for section in (play.watch, play.enter, play.exit, play.caveats):
                assert section, f"{play.name} 的 {section!r} 是空的"
            # 观察面与成立必须带具体条件；退出的条件形态多样（死叉、两根红柱、
            # 停止补格），由动作断言管——两道网合起来守住「可核对、可执行」
            for section in (play.watch, play.enter):
                assert any(
                    any(ch.isdigit() for ch in text)
                    or any(word in text for word in ("量", "MA", "止损", "止盈", "一格"))
                    for text in section
                ), f"{play.name} 有一条没有具体条件：{section}"

    def test_every_play_writes_down_when_it_is_wrong(self):
        """每个战法必须写明「什么时候承认错了」——退出机制是战法的一半。"""
        for play in PLAYS:
            assert play.exit, f"{play.name} 没有退出机制：只讲进不讲出等于没有话术"

    def test_exit_mechanisms_mention_concrete_consequences(self):
        for play in PLAYS:
            joined = " ".join(play.exit)
            assert any(
                word in joined
                for word in (
                    "止损",
                    "减",
                    "撤",
                    "收掉",
                    "清",
                    "不接",
                    "不再",
                    "停止",
                    "结束",
                    "出场",
                )
            ), f"{play.name} 的退出机制没有可执行动作"

    def test_no_win_rate_claims_anywhere(self):
        """没回测过就不给胜率数字——「胜率低赔率高」这种风险描述可以，「胜率 70%」不行。"""
        for play in PLAYS:
            for text in (*play.watch, *play.enter, *play.exit, *play.caveats, play.one_liner):
                assert not re.search(r"胜率[达超过约为]*\s*[\d.]+\s*%?", text), (
                    f"{play.name} 里出现了胜率数字：{text}"
                )
                assert "成功率达" not in text and "命中率高" not in text


class TestTodayRules:
    def test_first_red_on_a_strong_yesterday_reads_as_go(self):
        payload = analyze(SUNNY, {"label": "分化", "score": 0.2})
        status, note = (
            _by_key(payload, "first-red")["status"],
            _by_key(payload, "first-red")["today_note"],
        )

        assert status == "顺"
        assert "一红更可信" in note

    def test_first_red_after_a_volume_selloff_is_avoided(self):
        payload = analyze(GLOOMY, None)

        play = _by_key(payload, "first-red")

        assert play["status"] == "避"
        assert "反抽" in play["today_note"]

    def test_first_red_on_dry_volume_rally_is_cautioned(self):
        assert (
            analyze({"quadrant": "缩量上涨", "ratio": 0.7, "level": "地量", "streak": 0}, None)[
                "plays"
            ][0]["status"]
            == "慎"
        )

    def test_overseas_risk_on_warms_the_open_play(self):
        payload = analyze(SUNNY, {"label": "风险偏好回升", "score": 1.2})

        assert "高开出货" in _by_key(payload, "first-red")["today_note"]

    def test_ground_volume_play_waits_for_dry_days_only(self):
        assert _by_key(analyze(DRY, None), "ground-volume")["status"] == "顺"
        assert (
            _by_key(
                analyze({"quadrant": "放量上涨", "ratio": 1.6, "level": "放量", "streak": 1}, None),
                "ground-volume",
            )["status"]
            == "歇"
        )  # 量能充沛的日子这条战法无事可做——不是「不适用」，是「歇」

    def test_volume_discipline_always_applies(self):
        for env in (SUNNY, GLOOMY, DRY, BALANCED):
            assert _by_key(analyze(env, None), "volume-discipline")["status"] == "顺"

    def test_sector_leader_is_avoided_when_the_market_sells_off(self):
        assert _by_key(analyze(GLOOMY, None), "sector-leader")["status"] == "避"
        assert _by_key(analyze(SUNNY, None), "sector-leader")["status"] == "顺"

    def test_gap_trap_reads_the_overseas_tone_both_ways(self):
        warm = _by_key(analyze(BALANCED, {"label": "风险偏好回升", "score": 1.0}), "gap-trap")
        cold = _by_key(analyze(BALANCED, {"label": "风险偏好回落", "score": -1.0}), "gap-trap")

        assert "高开 >4%" in warm["today_note"]
        assert "低开" in cold["today_note"]

    def test_missing_environment_is_honest_not_a_guess(self):
        payload = analyze(None, None)

        assert _by_key(payload, "first-red")["status"] is None
        assert "数据缺失" in _by_key(payload, "first-red")["today_note"]
        # 战法本体照给：环境缺失不等于战法失效
        assert payload["n_plays"] == len(PLAYS)
        assert "缺失" in payload["environment"]["summary"]

    def test_grid_stays_out_of_downtrends_not_just_downtrends_with_volume(self):
        # 缩量下跌也是单边：还在寻底，区间下沿可能不保
        assert _by_key(analyze(DRY, None), "grid")["status"] == "慎"
        assert _by_key(analyze(BALANCED, None), "grid")["status"] == "顺"

    def test_form_plays_do_not_pretend_to_read_the_environment(self):
        # 仙人指路、缺口回补、MACD 是个股 K 线上的形态：不挂环境规则
        for key in ("immortal-guide", "gap-fill", "macd-second-cross"):
            play = _by_key(analyze(SUNNY, None), key)
            assert play["status"] is None
            assert "不依赖" in play["today_note"] or "个股" in play["today_note"]

    def test_first_board_is_avoided_in_cold_markets(self):
        assert _by_key(analyze(DRY, None), "first-board")["status"] == "避"
        assert _by_key(analyze(SUNNY, None), "first-board")["status"] == "慎"

    def test_turtle_breakout_follows_the_market_volume(self):
        assert _by_key(analyze(GLOOMY, None), "turtle")["status"] == "避"
        assert _by_key(analyze(SUNNY, None), "turtle")["status"] == "顺"

    def test_the_catalog_now_carries_the_classic_public_plays(self):
        names = {play.name for play in PLAYS}
        assert {
            "海龟突破（唐奇安通道）",
            "MACD 二次金叉",
            "W 底（双重底）",
            "黄金坑",
            "仙人指路",
            "缺口回补",
            "网格交易",
            "首板打板（涨停首板）",
        } <= names
        assert len(PLAYS) == 16

    def test_summary_quotes_both_sources_when_available(self):
        summary = analyze(SUNNY, {"label": "风险偏好回落", "score": -0.8})["environment"]

        assert "放量上涨" in summary["summary"]
        assert "风险偏好回落" in summary["summary"]


class TestAPI:
    @pytest.fixture()
    def pb_client(self, tmp_path, monkeypatch) -> TestClient:
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Data:
            directory = tmp_path

        class _Collection:
            provider = "baostock"
            adjust = "qfq"

        class _Settings:
            data = _Data()
            collection = _Collection()

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        return TestClient(create_app())

    def test_playbook_comes_back_even_without_any_data(self, pb_client):
        """面板和外围都缺：返回 8 条战法与诚实的「数据缺失」标注，而不是 500。"""
        payload = pb_client.get("/api/playbook").json()

        assert payload["n_plays"] == len(PLAYS)
        assert payload["environment"]["as_of"] is None
        assert _by_key(payload, "first-red")["status"] is None

    def test_playbook_rides_the_real_analysis_outputs(self, pb_client, monkeypatch):
        import pandas as pd

        from quant_research.data.loader import PricePanel
        from quant_research.web import api as api_module

        index = pd.bdate_range("2024-01-01", periods=30)
        prices = pd.DataFrame({"600519.SH": [10.0] * 30}, index=index)
        panel = PricePanel(prices=prices, extra={"amount": prices * 1e6, "volume": prices * 1e5})
        monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: panel)

        payload = pb_client.get("/api/playbook").json()

        assert payload["environment"]["as_of"] == "2024-02-09"
        assert _by_key(payload, "first-red")["status"] is not None


class TestPlaybookVerify:
    """可编码战法的回测验证：把「胜率未知」变成「本样本上的轮廓」——
    前提是数字真的来自本样本；从未触发就如实说未触发，不编曲线。"""

    @pytest.fixture()
    def pv_client(self, tmp_path, monkeypatch) -> TestClient:
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Data:
            directory = tmp_path

        class _Collection:
            provider = "baostock"
            adjust = "qfq"

        class _Backtest:
            initial_cash = 100000
            commission_bps = 5.0
            slippage_bps = 2.0
            allow_short = False

        class _Settings:
            data = _Data()
            collection = _Collection()
            backtest = _Backtest()

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        return TestClient(create_app())

    @staticmethod
    def _panel(rows: int = 30, symbols: int = 3):
        """横盘 + 末日放量跳升：海龟与放量突破确定性触发。"""
        import pandas as pd

        from quant_research.data.loader import PricePanel

        index = pd.bdate_range("2024-01-01", periods=rows)
        steps = np.concatenate([np.full(rows - 1, 100.0), [120.0]])
        prices = pd.DataFrame({f"S{step}": steps for step in range(symbols)}, index=index)
        amounts = np.full(rows, 1e6)
        amounts[-1] = 3e6  # 量比 3：放量条件达标
        amount = pd.DataFrame({f"S{step}": amounts for step in range(symbols)}, index=index)
        return PricePanel(prices=prices, extra={"amount": amount, "volume": amount / 10})

    def test_verify_scores_every_codable_play_on_this_sample(self, pv_client, monkeypatch):
        from quant_research.web import api as api_module

        panel = self._panel(rows=30)
        monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: panel)

        payload = pv_client.post("/api/playbook/verify").json()

        assert payload["available"] is True
        assert payload["n_symbols"] == 3 and payload["n_days"] == 30
        keys = {row["key"] for row in payload["results"]}
        assert keys == {"turtle", "breakout-20d-high", "pullback-ma20"}
        scored = {row["key"]: row for row in payload["results"] if row["available"]}
        # 横盘 + 末日放量跳升：海龟与放量突破确定性触发
        assert "turtle" in scored and "breakout-20d-high" in scored
        for row in scored.values():
            metrics = row["metrics"]
            assert metrics["cumulative_return"] is not None
            assert metrics["win_rate"] is not None
            assert row["n_symbols_with_signal"] >= 1

    def test_untriggered_play_reads_as_untriggered_not_zero_return(self, pv_client, monkeypatch):
        """单边下跌面板喂给海龟：从不创 20 日新高就从不入场——
        回测必须如实说「未触发」，而不是给一条空转的净值曲线。"""
        import pandas as pd

        from quant_research.data.loader import PricePanel
        from quant_research.web import api as api_module

        rows = 40
        index = pd.bdate_range("2024-01-01", periods=rows)
        prices = pd.DataFrame(
            {
                "S0": np.linspace(120.0, 80.0, rows),
                "S1": np.linspace(100.0, 60.0, rows),
                "S2": np.linspace(90.0, 70.0, rows),
            },
            index=index,
        )
        panel = PricePanel(prices=prices, extra={"amount": prices * 1e6, "volume": prices * 1e5})
        monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: panel)

        payload = pv_client.post("/api/playbook/verify").json()

        by_key = {row["key"]: row for row in payload["results"]}
        assert by_key["turtle"]["available"] is False
        assert "未触发" in by_key["turtle"]["reason"]

    def test_without_a_panel_the_400_names_the_prerequisite(self, pv_client, monkeypatch):
        from fastapi import HTTPException

        from quant_research.web import api as api_module

        def no_panel(*args, **kwargs):
            raise HTTPException(status_code=404, detail="没有缓存面板")

        monkeypatch.setattr(api_module, "_load_panel", no_panel)

        response = pv_client.post("/api/playbook/verify")

        assert response.status_code == 400
        assert "面板不可用" in response.json()["detail"]
