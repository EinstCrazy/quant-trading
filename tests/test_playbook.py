"""战法：清单结构、今日环境标注规则、API 的「缺数据照给战法」约定。

守住的几条：不出现胜率数字（没回测过就不假装有）；「今日顺/慎/避」由
量价四象限与外围情绪两个客观状态推出，规则可读可测；环境缺一块就少一块
标注，战法本体照给——「数据不足」要和「无事可做」分开说。
"""

from __future__ import annotations

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
            # 观察面、成立、退出三节必须带具体条件（注意事项是劝告，不要求数字）
            for section in (play.watch, play.enter, play.exit):
                assert any(
                    any(ch.isdigit() for ch in text) or "量" in text or "MA" in text
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
                word in joined for word in ("止损", "减", "撤", "收掉", "清", "不接", "不再")
            ), f"{play.name} 的退出机制没有可执行动作"

    def test_no_win_rate_claims_anywhere(self):
        """没回测过就不给胜率——价位百分比可以有，胜率表述不可以有。"""
        for play in PLAYS:
            for text in (*play.watch, *play.enter, *play.exit, *play.caveats, play.one_liner):
                assert "胜率" not in text and "成功率达" not in text and "命中" not in text, (
                    f"{play.name} 里出现了胜率式表述：{text}"
                )


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
            is None
        )  # 量能充沛的日子这条战法无事可做

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
