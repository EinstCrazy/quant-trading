class TestCompactDetail:
    """存档保存详细信息：只有档位标签的历史无法回溯「那天为什么这么判」。

    依据（RSI/均线/价位/预案）与组合层（summary/discipline/plan）都要
    留下——依然是快照数字，不是原始 K 线。
    """

    @staticmethod
    def _payload() -> dict:
        return {
            "as_of": "2026-11-17",
            "provider": "baostock",
            "user_note": " 明天先看止损区那两只 ",
            "summary": {
                "note": "1 只处于止损区",
                "total_value": 50000.0,
                "total_pnl": -800.0,
                "total_pnl_pct": -1.6,
                "counts": {"hold": 3, "reduce": 1},
                "concentration": 35.2,
            },
            "discipline": [{"rule": "max_single_pct", "ok": True}],
            "position_plan": {"note": "留 20% 现金"},
            "positions": [
                {
                    "symbol": "588350.SH",
                    "account": "ycc",
                    "stance": "reduce",
                    "close": 1.102,
                    "pnl_pct": -4.3,
                    "shares": 50100,
                    "cost": 1.15,
                    "market_value": 55210.2,
                    "pnl": -2404.85,
                    "change_pct": -2.9,
                    "rsi_14": 41.5,
                    "ma20": 1.18,
                    "ma60": 1.21,
                    "atr_14": 0.03,
                    "high20": 1.25,
                    "low20": 1.09,
                    "volume_ratio": 1.4,
                    "headline": "跌破 MA20 且跌破成本",
                    "reasons": ["收盘低于 MA20", "亏损超过阈值"],
                    "levels": {"stop": 1.05, "take": 1.30},
                    "plans": [{"when": "跌破 1.05", "then": "减半仓"}],
                },
                {
                    "symbol": "600742.SH",
                    "account": "ycc",
                    "stance": "hold",
                    "close": 7.8,
                    "pnl_pct": None,
                },
            ],
        }

    def test_entry_keeps_evidence_fields(self, tmp_path):
        """档位条目保留依据字段；缺字段不冒充（600742 只有 close）。"""
        from quant_research import review_archive as review_store

        review_store.save(tmp_path, self._payload())
        data = review_store.load(tmp_path, "2026-11-17")
        entry = data["positions"]["ycc::588350.SH"]
        assert entry["rsi_14"] == 41.5
        assert entry["ma20"] == 1.18
        assert entry["headline"] == "跌破 MA20 且跌破成本"
        assert entry["levels"] == {"stop": 1.05, "take": 1.30}
        assert entry["plans"] == [{"when": "跌破 1.05", "then": "减半仓"}]
        # 没有依据的票不编造字段
        plain = data["positions"]["ycc::600742.SH"]
        assert "rsi_14" not in plain
        assert "headline" not in plain

    def test_top_level_keeps_portfolio_layers(self, tmp_path):
        """组合层三块（summary/discipline/position_plan）整体落档。"""
        from quant_research import review_archive as review_store

        review_store.save(tmp_path, self._payload())
        data = review_store.load(tmp_path, "2026-11-17")
        assert data["summary"]["total_value"] == 50000.0
        assert data["summary"]["counts"] == {"hold": 3, "reduce": 1}
        assert data["discipline"] == [{"rule": "max_single_pct", "ok": True}]
        assert data["position_plan"] == {"note": "留 20% 现金"}
        assert data["user_note"] == "明天先看止损区那两只"

    def test_changes_still_compare_on_stance(self, tmp_path):
        """老存档（无依据字段）与新存档对比不炸：stance 对比不受影响。"""
        from quant_research import review_archive as review_store

        first = self._payload()
        first["as_of"] = "2026-11-16"
        first["positions"] = [
            {k: v for k, v in row.items() if k not in ("rsi_14", "headline", "levels")}
            for row in first["positions"]
        ]
        review_store.save(tmp_path, first)
        second = self._payload()
        second["positions"][0]["stance"] = "hold"
        review_store.save(tmp_path, second)
        data = review_store.load(tmp_path, "2026-11-17")
        assert data["date"] == "2026-11-17"
