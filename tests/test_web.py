"""Web API contract tests.

The UI is a shell over the library, so what matters here is the contract: every
number must come from the library and every input error must be a clear 4xx.
Skipped entirely when the ``web`` extra is not installed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SAMPLE = "sample:sample_prices"


@pytest.fixture()
def client(tmp_path):
    pytest.importorskip("fastapi", reason="web extra not installed")
    from fastapi.testclient import TestClient

    from quant_research.web import create_app
    from quant_research.web.experiments import reset_store as reset_experiments
    from quant_research.web.jobs import JobStore
    from quant_research.web.jobs import reset_store as reset_jobs

    # A backtest records an experiment and archives a job; point both at a
    # scratch directory so tests never write into the repository.
    reset_jobs(JobStore(archive_dir=tmp_path / "webui"))
    reset_experiments(tmp_path / "experiments.jsonl")
    return TestClient(create_app())


def test_health_lists_registered_strategies(client):
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert "ma_cross" in payload["strategies"]


def test_datasets_expose_the_sample_file(client):
    datasets = client.get("/api/datasets").json()
    keys = [item["key"] for item in datasets]
    assert SAMPLE in keys
    assert all(item["path"] for item in datasets)


def test_overview_reports_meta_and_quality(client):
    payload = client.get(f"/api/datasets/{SAMPLE}/overview").json()

    assert payload["meta"]["n_symbols"] == 3
    assert payload["quality"]["n_obs"] > 100
    assert payload["quality"]["is_clean"] is True
    assert payload["preview"]["rows"]
    assert len(payload["series"]) == 3


def test_overview_rejects_unknown_dataset(client):
    response = client.get("/api/datasets/nope:nope/overview")
    assert response.status_code == 404
    assert "unknown dataset" in response.json()["detail"]


def test_backtest_returns_curves_metrics_and_trades(client):
    response = client.post(
        "/api/backtest",
        json={
            "dataset": SAMPLE,
            "strategy": "ma_cross",
            "params": {"fast_window": 20, "slow_window": 50},
            "weight_method": "equal_weight",
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["metrics"]["sharpe"] is not None
    assert payload["metrics"]["n_periods"] > 100
    assert len(payload["equity"]) > 100
    assert len(payload["benchmark"]) == len(payload["equity"])
    assert payload["drawdown"][0][1] == 0.0  # no drawdown on day one
    assert payload["trade_count"] > 0
    assert payload["trades"][0]["symbol"]
    assert payload["final_equity"] > 0


def test_backtest_honours_open_execution(client):
    payload = client.post(
        "/api/backtest",
        json={
            "dataset": SAMPLE,
            "strategy": "ma_cross",
            "params": {"fast_window": 10, "slow_window": 30},
            "exec_price": "open",
        },
    ).json()
    assert payload["trade_count"] > 0
    assert payload["config"]["execution"]["price"] == "open"


def test_backtest_rejects_unknown_strategy(client):
    response = client.post("/api/backtest", json={"dataset": SAMPLE, "strategy": "nope"})
    assert response.status_code == 404
    assert "unknown strategy" in response.json()["detail"]


def test_backtest_rejects_bad_dates(client):
    response = client.post("/api/backtest", json={"dataset": SAMPLE, "start": "01/02/2024"})
    assert response.status_code == 400


def test_compare_returns_baseline_and_variants(client):
    response = client.post(
        "/api/backtest/compare",
        json={
            "base": {
                "dataset": SAMPLE,
                "strategy": "ma_cross",
                "params": {"fast_window": 10, "slow_window": 30},
                "weight_method": "equal_weight",
            },
            "variants": [{"label": "慢线 100", "params": {"slow_window": 100}}],
        },
    )
    assert response.status_code == 200
    rows = response.json()

    assert [row["label"] for row in rows] == ["base", "慢线 100"]
    assert rows[0]["metrics"]["cumulative_return"] != rows[1]["metrics"]["cumulative_return"]
    assert all(row["equity"] for row in rows)


def test_collection_status_reports_the_root(client):
    payload = client.get("/api/collection/status").json()
    assert payload["root"]
    assert isinstance(payload["providers"], list)


def test_collect_dry_run_writes_nothing(client):
    response = client.post(
        "/api/collection/collect",
        json={
            "provider": "mock",
            "symbols": ["600000.SH"],
            "start": "2024-01-02",
            "end": "2024-01-31",
            "dry_run": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["provider"] == "mock"
    assert payload["updated"] == {"600000.SH": 22}
    assert payload["summary"]["failed"] == 0


def test_collect_rejects_unknown_provider(client):
    response = client.post(
        "/api/collection/collect", json={"provider": "nope", "symbols": ["600000.SH"]}
    )
    assert response.status_code == 400
    # "失败" is not a reason: the name the caller typed must come back
    assert "nope" in response.json()["detail"]


def test_a_failed_sweep_keeps_the_reason(client, monkeypatch):
    """A gateway that refuses to list is a reason, not a bare failure."""
    from quant_research.data.providers.base import ProviderError
    from quant_research.data.providers.mock import MockProvider

    def broken(self):
        raise ProviderError("gateway refused the connection")

    monkeypatch.setattr(MockProvider, "list_symbols", broken)

    response = client.post(
        "/api/collection/collect", json={"provider": "mock", "all_symbols": True}
    )

    assert response.status_code == 502
    assert "gateway refused the connection" in response.json()["detail"]


def test_a_failed_job_keeps_the_reason(client, monkeypatch):
    """The job card has one line worth reading; it must not be just 'failed'."""
    from quant_research.data.providers.base import SourceUnreachable
    from quant_research.web import api as api_module

    def boom(**kwargs):
        raise SourceUnreachable("could not reach OpenD at 127.0.0.1:11111")

    monkeypatch.setattr(api_module, "collect_once", boom)

    ref = client.post(
        "/api/jobs/collect", json={"provider": "mock", "symbols": ["600000.SH"]}
    ).json()
    job = _wait(client, ref)

    assert job["status"] == "failed"
    assert "OpenD" in job["error"]


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "量化研究工作台" in response.text


def _wait(client, ref, attempts=100):
    """Poll a job until it settles; tests must not hang on a broken queue."""
    import time

    job = None
    for _ in range(attempts):
        job = client.get(f"/api/jobs/{ref['id']}").json()
        if job["status"] in {"done", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job did not settle: {job}")


def test_backtest_job_completes(client):
    submitted = client.post(
        "/api/jobs/backtest",
        json={
            "dataset": SAMPLE,
            "strategy": "ma_cross",
            "params": {"fast_window": 20, "slow_window": 50},
        },
    )
    assert submitted.status_code == 202
    ref = submitted.json()
    assert ref["kind"] == "backtest"

    job = _wait(client, ref)
    assert job["status"] == "done", job.get("error")
    assert job["progress"]["ratio"] == 1.0
    assert job["result"]["metrics"]["sharpe"] is not None
    assert job["result"]["trade_count"] > 0
    assert job["archive"]


def test_compare_job_counts_every_run(client):
    submitted = client.post(
        "/api/jobs/compare",
        json={
            "base": {
                "dataset": SAMPLE,
                "strategy": "ma_cross",
                "params": {"fast_window": 10, "slow_window": 30},
            },
            "variants": [{"label": "慢线 100", "params": {"slow_window": 100}}],
        },
    )
    assert submitted.status_code == 202
    ref = submitted.json()
    assert "2 组" in ref["label"]

    job = _wait(client, ref)
    assert job["status"] == "done", job.get("error")
    assert len(job["result"]) == 2


def test_collect_job_completes(client):
    submitted = client.post(
        "/api/jobs/collect",
        json={
            "provider": "mock",
            "symbols": ["600000.SH"],
            "start": "2024-01-02",
            "end": "2024-01-31",
            "dry_run": True,
        },
    )
    assert submitted.status_code == 202
    job = _wait(client, submitted.json())
    assert job["status"] == "done", job.get("error")
    assert job["result"]["summary"]["failed"] == 0


def test_collect_job_rejects_unknown_provider(client):
    response = client.post("/api/jobs/collect", json={"provider": "nope", "symbols": ["600000.SH"]})
    assert response.status_code == 400


def test_job_listing_and_unknown_ids(client):
    assert isinstance(client.get("/api/jobs").json(), list)
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.delete("/api/jobs/nope").status_code == 404


def test_jobstore_cancel_stops_a_running_task():
    import threading
    import time

    from quant_research.web.jobs import JobStore, reset_store

    store = JobStore(workers=1, archive_dir=None)
    reset_store(store)

    started = threading.Event()

    def slow(progress, stop):
        started.set()
        for index in range(500):
            if stop():
                return {"stopped_at": index}
            time.sleep(0.01)
        return {"stopped_at": None}

    job = store.submit("test", "slow task", slow, total=500)
    assert started.wait(5), "task never started"
    assert store.cancel(job.id)

    for _ in range(200):
        if store.get(job.id).status in {"done", "cancelled", "failed"}:
            break
        time.sleep(0.02)

    assert store.get(job.id).status == "cancelled"
    assert store.get(job.id).result["stopped_at"] is not None


def test_archived_jobs_survive_a_restart(tmp_path):
    import time

    from quant_research.web.jobs import JobStore, reset_store

    first = JobStore(archive_dir=tmp_path / "webui")
    reset_store(first)
    submitted = first.submit(
        "backtest", "sample · ma_cross", lambda progress, stop: {"sharpe": 0.5}
    )

    for _ in range(100):
        if first.get(submitted.id).status in {"done", "failed"}:
            break
        time.sleep(0.02)
    assert first.get(submitted.id).archive, "job was not archived"

    # a fresh process: memory is empty, the archive directory is not
    restored = JobStore(archive_dir=tmp_path / "webui")
    listed = restored.list()
    assert [job.id for job in listed] == [submitted.id]
    assert listed[0].archived is True
    assert listed[0].status == "done"  # not the half-written "running"
    assert listed[0].result is None  # metadata only
    assert restored.get(submitted.id).result == {"sharpe": 0.5}  # loaded on demand


def test_collection_reports_lists_archived_runs(client, tmp_path, monkeypatch):
    from datetime import date

    from quant_research.collector import collect_once, write_report
    from quant_research.data.providers import MockProvider
    from quant_research.data.store import LocalMarketStore

    report = collect_once(
        provider=MockProvider(symbols=("600000.SH",)),
        store=LocalMarketStore(tmp_path / "raw", "mock"),
        symbols=["600000.SH"],
        end=date(2024, 1, 31),  # a Wednesday
        dry_run=True,
    )
    write_report(report, str(tmp_path))

    from dataclasses import replace

    import quant_research.web.api as api_module

    settings = api_module.load_settings()
    patched = replace(settings, collection=replace(settings.collection, report_directory=tmp_path))
    monkeypatch.setattr(api_module, "load_settings", lambda: patched)

    payload = client.get("/api/collection/reports").json()
    assert len(payload["reports"]) == 1
    item = payload["reports"][0]
    assert item["provider"] == "mock"
    assert item["failed"] == 0
    assert item["failed_symbols"] == []


def test_backtest_records_an_experiment(client):
    client.post(
        "/api/backtest",
        json={
            "dataset": SAMPLE,
            "strategy": "ma_cross",
            "params": {"fast_window": 20, "slow_window": 50},
        },
    )
    payload = client.get("/api/experiments").json()
    assert len(payload["experiments"]) == 1
    item = payload["experiments"][0]
    assert item["dataset"] == SAMPLE
    assert item["strategy"] == "ma_cross"
    assert item["params"] == {"fast_window": 20, "slow_window": 50}
    assert item["metrics"]["sharpe"] is not None
    assert item["job_id"] is None  # a synchronous run leaves nothing to reopen


def test_backtest_job_experiment_points_at_the_job(client):
    ref = client.post(
        "/api/jobs/backtest",
        json={
            "dataset": SAMPLE,
            "strategy": "ma_cross",
            "params": {"fast_window": 20, "slow_window": 50},
        },
    ).json()
    assert _wait(client, ref)["status"] == "done"

    payload = client.get("/api/experiments").json()
    assert len(payload["experiments"]) == 1
    assert payload["experiments"][0]["job_id"] == ref["id"]


def test_experiments_sort_by_a_metric(client):
    for slow in (30, 100):
        client.post(
            "/api/backtest",
            json={
                "dataset": SAMPLE,
                "strategy": "ma_cross",
                "params": {"fast_window": 10, "slow_window": slow},
            },
        )

    ranked = client.get("/api/experiments?sort=sharpe").json()["experiments"]
    assert len(ranked) == 2
    sharpes = [item["metrics"]["sharpe"] for item in ranked]
    assert sharpes == sorted(sharpes, reverse=True)


def test_experiments_can_be_annotated_and_dropped(client):
    client.post(
        "/api/backtest",
        json={
            "dataset": SAMPLE,
            "strategy": "ma_cross",
            "params": {"fast_window": 20, "slow_window": 50},
        },
    )
    item = client.get("/api/experiments").json()["experiments"][0]

    patched = client.patch(f"/api/experiments/{item['id']}", json={"note": "成本吃掉收益"})
    assert patched.status_code == 200
    assert patched.json()["note"] == "成本吃掉收益"

    assert client.delete(f"/api/experiments/{item['id']}").json()["deleted"] is True
    assert client.get("/api/experiments").json()["experiments"] == []
    assert client.patch(f"/api/experiments/{item['id']}", json={"note": "x"}).status_code == 404


def test_grid_job_ranks_every_combination(client):
    ref = client.post(
        "/api/jobs/grid",
        json={
            "dataset": SAMPLE,
            "strategy": "ma_cross",
            "params": {"fast_window": [10, 20], "slow_window": [30, 100]},
            "metric": "sharpe",
        },
    ).json()
    job = _wait(client, ref)
    assert job["status"] == "done"

    result = job["result"]
    assert (result["requested"], result["completed"]) == (4, 4)
    assert len(result["rows"]) == 4
    sharpes = [row["metrics"]["sharpe"] for row in result["rows"]]
    assert sharpes == sorted(sharpes, reverse=True)

    # a sweep is not just a table on screen: every run stays in the ledger
    assert len(client.get("/api/experiments").json()["experiments"]) == 4


def test_grid_rejects_empty_and_oversized(client):
    empty = client.post(
        "/api/jobs/grid", json={"dataset": SAMPLE, "strategy": "ma_cross", "params": {}}
    )
    assert empty.status_code == 422

    big = client.post(
        "/api/jobs/grid",
        json={
            "dataset": SAMPLE,
            "strategy": "ma_cross",
            "params": {
                "fast_window": list(range(1, 31)),
                "slow_window": list(range(31, 61)),
            },
        },
    )
    assert big.status_code == 422
    assert "limit" in big.json()["detail"]


def test_full_market_sweep_takes_the_universe_from_the_source(client):
    """A sweep asks the source what exists, rather than reusing what we stored."""
    from quant_research.data.providers import get_provider

    universe = get_provider("mock").list_symbols()
    response = client.post(
        "/api/collection/collect",
        json={"provider": "mock", "all_symbols": True, "dry_run": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested"] == len(universe)
    # and it owns up to its size, because a two-hour run should not be a surprise
    assert body["swept"]["symbols"] == len(universe)


def test_a_sweep_is_paced_but_a_targeted_run_is_not(client, monkeypatch):
    """Thousands of requests in a row are what gets a connection closed on us."""
    from quant_research.web import api

    captured: dict = {}
    real = api.collect_once

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(api, "collect_once", spy)

    swept = client.post(
        "/api/collection/collect",
        json={"provider": "mock", "all_symbols": True, "dry_run": True, "pause": 0.0},
    )
    assert swept.status_code == 200, swept.text
    assert captured["pause"] == api._MIN_SWEEP_PAUSE  # asked for none, still paced

    captured.clear()
    client.post(
        "/api/collection/collect",
        json={"provider": "mock", "symbols": ["600519.SH"], "dry_run": True, "pause": 0.0},
    )
    assert captured["pause"] == 0.0  # three symbols do not need pacing


def test_a_sweep_can_be_submitted_without_symbols(client):
    """The "nothing supplied and nothing stored" rule must not block a sweep."""
    response = client.post(
        "/api/jobs/collect", json={"provider": "mock", "all_symbols": True, "dry_run": True}
    )
    assert response.status_code == 202, response.text
    assert "全市场" in response.json()["label"]


def test_a_sweep_honours_cancellation_before_any_download(client, monkeypatch):
    """Listing the market cannot be interrupted, but nothing may be fetched after.

    Found the hard way: a request to stop during a 30-second listing used to be
    ignored, and the run charged on into thousands of downloads.
    """
    import time

    from quant_research.data.providers.mock import MockProvider

    def slow_list(self):
        # returns a fixed list: calling the real one here would recurse into
        # this patch, one 0.6s sleep per stack frame
        time.sleep(0.6)  # long enough to cancel while it is still listing
        return ["600000.SH", "600519.SH", "000001.SZ"]

    monkeypatch.setattr(MockProvider, "list_symbols", slow_list)

    ref = client.post(
        "/api/jobs/collect", json={"provider": "mock", "all_symbols": True, "dry_run": True}
    ).json()
    client.delete(f"/api/jobs/{ref['id']}")

    # generous window: earlier jobs may still hold a worker, and the pool is small
    for _ in range(80):
        job = client.get(f"/api/jobs/{ref['id']}").json()
        if job["status"] not in ("running", "pending"):
            break
        time.sleep(0.1)

    assert job["status"] == "cancelled"
    assert (job.get("result") or {}).get("requested") == 0  # nothing was downloaded


def test_bare_codes_are_completed_with_their_exchange():
    """The digits decide the exchange: 600519 is 600519.SH, no dot required."""
    from quant_research.web.schemas import BacktestRequest, CollectRequest

    backtest = BacktestRequest(dataset="x", symbols=["600519", "000001", "sh600036"])
    assert backtest.symbols == ["600519.SH", "000001.SZ", "600036.SH"]
    assert CollectRequest(symbols=["300750"]).symbols == ["300750.SZ"]
    # omitting symbols and passing null stay the same thing
    assert BacktestRequest(dataset="x").symbols is None


def test_an_unrecognisable_code_is_a_422_with_a_readable_reason():
    """HZ is not an exchange this project knows; say so instead of guessing."""
    from pydantic import ValidationError

    from quant_research.web.schemas import CollectRequest

    with pytest.raises(ValidationError, match="999999"):
        CollectRequest(symbols=["999999.HZ"])


class TestApiHelperContract:
    """前端 api() 封装的约定回归：body 与 method 的组合曾经一起坏过。

    事故：三参形式（显式 method）丢 body——持仓添加一直发空请求 422；
    两参 { method: "POST" } 的 options 写法被误当 body 序列化出去。
    统一约定：第二参永远是请求体（对象 JSON 化、FormData 原样），第三参
    是显式 method。这里用结构断言钉住：约定不许再出现两种语义并存。
    """

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_no_options_style_calls_remain(self):
        """api(path, { method: "POST" }) 是旧 options 约定——必须死绝。"""
        assert not re.search(r'api\([^,\n]+, \{ method: "POST" \}\)', self._html())

    def test_form_data_is_passed_through_untouched(self):
        """Excel 上传走 multipart：FormData 原样直传，不带默认 Content-Type。"""
        assert "body instanceof FormData" in self._html()

    def test_explicit_method_branch_carries_the_body(self):
        """三参形式必须带体——修复的起点就是这个分支丢了 body。"""
        assert (
            'options = { method: method || (body != null ? "POST" : "GET"), ...jsonBody }'
            in self._html()
        )


class TestReviewCharts:
    """复盘图表：走势图只画决策线，浮盈横条按绝对值缩放。

    事故预防：图上线一多，读者只会看见一片虚线——BOLL 三轨 + 四条 MA
    一起上，成本线与止损线就找不到了。这里钉住「只留 MA20/MA60」。
    """

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_review_card_has_chart_containers(self):
        html = self._html()
        for element_id in ("pf-chart-symbol", "pf-chart", "pf-chart-facts", "pf-pnl-bars"):
            assert f'id="{element_id}"' in html

    def test_chart_reuses_the_indicator_series_endpoint(self):
        """走势图复用 /indicators/{symbol}/series——不新增后端端点，
        口径与指标页一致（同一份面板、同一份 MA）。"""
        html = self._html()
        assert "/indicators/${encodeURIComponent(row.symbol)}/series?days=120" in html

    def test_only_decision_overlays_are_drawn(self):
        html = self._html()
        assert '["ma_20", "ma_60"].includes(line.field)' in html
        assert (
            "boll_mid"
            not in html.split("async function loadReviewChart()")[1].split(
                "function renderReview("
            )[0]
        )

    def test_reference_lines_are_cost_and_atr_stop(self):
        """图上两条虚线只能是成本与 ATR 止损：其余价位留在文字区。"""
        html = self._html()
        assert "[row.cost, row.levels?.atr_stop]" in html

    def test_chart_can_draw_candles_without_lines(self, tmp_path):
        """指标没算完时只有蜡烛：图仍要画出来，不能整块空白。"""
        import shutil
        import subprocess

        if shutil.which("node") is None:
            pytest.skip("没有 node，跳过前端沙盒验证")
        html = self._html()
        start = html.index("function chart(target, series, options = {})")
        end = html.index("function table(columns, rows)")
        chart_src = html[start:end]
        fmt = re.search(r"function fmt\([^)]*\)\s*\{.*?\n\}", html, re.DOTALL)
        colors = re.search(r"const COLORS\s*=\s*\[[^\]]*\];", html)
        assert fmt and colors
        script = tmp_path / "chart.js"
        script.write_text(
            colors.group(0)
            + "\n"
            + fmt.group(0)
            + "\n"
            + chart_src
            + """
const target = { innerHTML: "" };
chart(target, [], { height: 200, reference: [10.51, 12.95], candles: [
  ["2024-01-02", 10, 11, 9.5, 10.5],
  ["2024-01-03", 10.5, 11.5, 10.2, 11.2],
] });
if (!target.innerHTML.includes("<svg")) { console.error("NO_SVG"); process.exit(1); }
if ((target.innerHTML.match(/<rect/g) || []).length < 2) { console.error("NO_CANDLES"); process.exit(1); }
""",
            encoding="utf-8",
        )
        result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


class TestEtfDetailLink:
    """ETF 列表点一只进详情：同页详情区 + 可分享的哈希深链。

    不做第二个页面：同一份数据画两遍，迟早有一边先腐坏。列表行、名称、
    代码都可点；#/etf/510300 刷新与前进后退都能还原。
    """

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_snapshot_rows_are_clickable(self):
        html = self._html()
        assert '<tr data-etf-row="${row.symbol}">' in html
        assert 'data-etf-jump="${row.symbol}"' in html

    def test_hash_route_selects_the_etf(self):
        html = self._html()
        assert "ETF_PENDING = decodeURIComponent(arg)" in html
        assert 'if (name === "etf" && arg)' in html

    def test_jump_reuses_the_same_detail_section(self):
        """详情就是「走势与历史」卡：选中 + 滚动 + 行高亮，不新开页面。"""
        html = self._html()
        assert "function jumpToEtf(" in html
        assert "function selectEtfDetail(" in html
        assert "function highlightEtfRow(" in html
        assert 'row.classList.toggle("selected"' in html

    def test_pending_selection_is_cleared_when_nothing_is_collectable(self):
        """没有可选的标的时待选必须清掉，否则下次进页会莫名选中旧标的。"""
        html = self._html()
        section = html.split("function renderEtfHistory(")[1].split("async function refreshEtf(")[0]
        assert section.count("ETF_PENDING = null") >= 1

    def test_deep_link_does_not_double_fetch(self):
        """深链进入只取一次历史：选中与加载同一趟完成，不调两次 selectEtfDetail。"""
        html = self._html()
        section = html.split("function renderEtfHistory(")[1].split("async function refreshEtf(")[0]
        assert "selectEtfDetail(pending)" not in section
        assert "highlightEtfRow(pending)" in section


class TestEtfCompare:
    """多只 ETF 对比：归一化曲线 + 统计 + 相关性，上限 6 只。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_chips_and_compare_endpoint_are_wired(self):
        html = self._html()
        assert 'id="etf-cmp-chips"' in html
        assert "/etf/compare?symbols=" in html
        assert "function toggleEtfCompare(" in html

    def test_normalised_to_100_with_reference_line(self):
        """价格量级不同的标的直接画会被压成直线：首日=100 + 100 参考线。"""
        html = self._html()
        assert "reference: [100]" in html
        assert "首日=100" in html

    def test_six_curves_is_the_client_side_limit_too(self):
        """服务端挡 6 只，前端也要挡：不然点了没反应，用户以为坏了。"""
        html = self._html()
        assert "ETF_CMP.size >= 6" in html

    def test_single_selection_explains_why_there_is_no_chart(self):
        html = self._html()
        assert "选两只以上才能对比" in html


class TestEtfRefreshButtons:
    """ETF 更新数据的按钮：入口好找、失败点名、不重复请求。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_refresh_buttons_exist_in_both_cards(self):
        """更新数据必须有点得着的入口：详情卡与对比卡各一个，标题就写明会联网。"""
        html = self._html()
        assert 'id="etf-hist-refresh"' in html
        assert 'id="etf-cmp-refresh"' in html
        assert "更新这只（联网）" in html
        assert "更新选中（联网）" in html

    def test_single_refresh_reuses_the_returned_snapshot(self):
        """端点顺手带回快照：前端不再多打一次 /etf/snapshot。"""
        html = self._html()
        assert "result.snapshot || ETF_DATA" in html
        assert "result.snapshot || snapshot" in html

    def test_failed_symbols_are_named_not_silently_skipped(self):
        html = self._html()
        assert "部分失败：" in html


class TestAccountCrudUI:
    """账户增删改查的界面：行内编辑 + 账户重命名/删除按钮。

    编辑不能靠「删了重加」——界面上必须有点得着的「编辑」。
    """

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_inline_edit_row_inputs(self):
        html = self._html()
        for input_id in ("pf-e-account", "pf-e-shares", "pf-e-cost", "pf-e-note"):
            assert f'id="{input_id}"' in html
        assert 'data-pf-edit="${p.symbol}"' in html
        assert "data-pf-save" in html and "data-pf-cancel" in html

    def test_edit_goes_through_the_patch_endpoint(self):
        html = self._html()
        assert '"PATCH"' in html
        assert "to_account" in html  # 行内可挪账户

    def test_account_rename_and_drop_buttons(self):
        html = self._html()
        assert 'id="pf-account-rename"' in html
        assert 'id="pf-account-drop"' in html
        assert "/portfolio/accounts/rename" in html
        assert "/portfolio/accounts/" in html  # DELETE 路径
        # 重命名说明「存档一并迁移」、删除说明「存档保留」——口径要明说
        assert "复盘存档一并迁移" in html
        assert "复盘存档保留" in html

    def test_rename_and_drop_require_a_specific_account(self):
        """「全部账户」不能当操作对象：点了要提示，不能静默乱来。"""
        html = self._html()
        assert html.count("「全部账户」不能") >= 2


class TestScreenFormResilience:
    """「跑一次选股」不再 null 报错：表单缺失时自愈，启动失败不拖死全页。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_provider_access_goes_through_the_healing_helper(self):
        """直接取 $("scr-provider").value 在表单缺失时就是 null 报错——
        三处一律走 screenProvider()：缺失时先重建，再兜底 baostock。"""
        html = self._html()
        assert "function screenProvider()" in html
        assert 'if (!$("scr-provider")) buildScreenForm();' in html
        assert 'return $("scr-provider") ? $("scr-provider").value : "baostock";' in html
        body = html[
            html.index("function screenProvider()") : html.index("function buildScreenForm")
        ]
        # 三元保护里的合法取值排除后，其余裸取值不许回来
        assert '$("scr-provider").value' not in body.replace(
            'return $("scr-provider") ? $("scr-provider").value : "baostock";', ""
        )

    def test_call_sites_use_the_helper(self):
        """runScreen / loadScreenLatest / loadScreenReview 全走 helper。"""
        html = self._html()
        assert "provider: screenProvider()" in html
        assert '"/screen/latest?provider=" + screenProvider()' in html
        assert "const provider = screenProvider();" in html
        # 帮助函数之外不允许再裸取 scr-provider 的 value
        protected = html[html.index("function buildScreenForm") :]
        assert '$("scr-provider").value' not in protected.replace(
            'return $("scr-provider") ? $("scr-provider").value : "baostock";', ""
        )

    def test_start_isolates_each_step(self):
        """启动逐步隔离：一步失败只在控制台留痕，表单构建照常执行。"""
        html = self._html()
        start = html[html.index("(async function start()") :]
        assert "for (const step of steps)" in start
        assert "catch (error)" in start
        # 表单构建必须在 steps 列表里（而不是独立裸调用）
        assert "buildScreenForm," in start


class TestAccountFirstUI:
    """录入时账户只选不打字：select + 显式「新建账户」入口。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_entry_account_is_a_select_not_free_text(self):
        """录入时的账户不许手打名字——手打是「华泰」和「华泰证券 」分裂的来源。"""
        html = self._html()
        assert '<select id="pf-account"' in html
        assert 'input id="pf-account"' not in html
        assert 'id="pf-e-account" list=' not in html  # 行内编辑挪账户同样只选

    def test_explicit_create_button_exists(self):
        html = self._html()
        assert 'id="pf-account-new"' in html
        assert '"/portfolio/accounts"' in html  # POST 创建
        assert "先建户、后录入" in html


class TestScreenProminent:
    """选完股显眼可见：名单卡置顶 + 高亮 + 大字摘要——跑选股就是为了看
    这个，不该让它蹲在表单下面等滚动条。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_latest_card_moves_above_the_form(self):
        html = self._html()
        render = html[
            html.index("function renderScreen(data)") : html.index("function scrollToScreenLatest")
        ]
        assert "insertBefore" in render
        assert 'view.querySelector(".card")' in render

    def test_headline_with_top_pick(self):
        """大字摘要：几只、首选是谁、多少权重——一眼看完。"""
        html = self._html()
        assert 'class="scr-headline"' in html
        render = html[
            html.index("function renderScreen(data)") : html.index("function scrollToScreenLatest")
        ]
        assert "今日选出" in render and "首选" in render

    def test_hot_style_only_when_picks_exist(self):
        html = self._html()
        assert "#scr-latest.scr-hot" in html
        render = html[
            html.index("function renderScreen(data)") : html.index("function scrollToScreenLatest")
        ]
        assert (
            'classList.toggle("scr-hot", Boolean(data && data.picks && data.picks.length))'
            in render
        )

    def test_run_scrolls_to_the_list(self):
        """跑完自动滚到名单——不用自己找。"""
        html = self._html()
        assert "scrollToScreenLatest();" in html[html.index("async function runScreen()") :]


class TestPositionPlanUI:
    """复盘卡的「明日仓位预案」：开盘位置 × 量能 → 动作，文案必须转义。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_plan_block_exists_and_is_wired(self):
        html = self._html()
        assert 'id="pf-plan"' in html
        assert "function renderReviewPlan(plan)" in html
        assert "renderReviewPlan(REVIEW.position_plan)" in html
        # 提前 return 的分支也要清空，不然上次的数据残留
        assert html.count("renderReviewPlan(null)") >= 2

    def test_branch_text_is_escaped(self):
        """预案文案进 innerHTML 前必须过转义——纪律和防注入同一条线。"""
        html = self._html()
        render = html[html.index("function renderReviewPlan(plan)") :]
        render = render[: render.index("\nfunction renderReview()")]
        assert "const esc = (text)" in render
        assert "${esc(text)}" in render

    def test_payload_carries_the_plan(self):
        """/portfolio/review 响应里必须带 position_plan（不可用时为 null）。"""
        from pathlib import Path as _P

        api = (_P(__file__).parents[1] / "src/quant_research/web/api.py").read_text(
            encoding="utf-8"
        )
        assert '"position_plan": plan' in api
        assert api.count('"position_plan": None') >= 2


class TestVolAxis:
    """大盘量能柱状图加日期轴：肉眼看趋势得有坐标系，不能全靠悬停。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_axis_aligns_with_bars(self):
        """日期轴与柱同构（每柱一格，flex 对齐）——错位一格日期就全对错了。"""
        html = self._html()
        assert 'class="volaxis"' in html
        assert "const step = n <= 10 ? 1 : n <= 20 ? 2 : 5" in html
        assert "visibility:hidden" in html  # 隐藏格占位，保持对齐

    def test_axis_shows_first_and_last(self):
        """首尾必显：起止日期一眼可见，中间隔根稀疏。"""
        html = self._html()
        render = html[
            html.index("const step = n <= 10") : html.index(
                "target.innerHTML = `", html.index("const step = n <= 10")
            )
        ]
        assert "i % step === 0 || i === n - 1" in render
        assert "String(point.date).slice(5)" in render  # MM-DD，年份省略


class TestEtfCheckCompare:
    """ETF 清单勾选框 → 对比：选择权在清单里，不用再跑到对比卡点一遍。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_list_rows_have_checkboxes(self):
        """清单每行一个勾选框，状态来自同一份 ETF_CMP——与选择片联动。"""
        html = self._html()
        assert 'data-etf-check="${row.symbol}"' in html
        assert "对比</th>" in html
        assert 'ETF_CMP.has(row.symbol) ? "checked" : ""' in html

    def test_checkbox_click_does_not_jump_to_detail(self):
        """勾选框点击不该顺带跳详情——两个意图混在一个点击里就是误操作。"""
        html = self._html()
        assert 'if (event.target.closest("[data-etf-check]")) return;' in html

    def test_change_drives_compare(self):
        html = self._html()
        assert 'document.addEventListener("change"' in html
        assert "一次最多对比 6 只" in html[html.index('document.addEventListener("change"') :]

    def test_seed_only_once(self):
        """用户清空勾选是想自己挑——默认 3 只只种一次，不偷偷塞回。"""
        html = self._html()
        assert "ETF_CMP_SEEDED" in html
        assert "if (!ETF_CMP.size && !ETF_CMP_SEEDED)" in html


class TestBreadthUI:
    """大盘数据里的广度：量能卡占比条 + 概览行，缺快照给采集指引。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_volume_card_shows_breadth(self):
        html = self._html()
        assert "function breadthHtml(b)" in html
        assert "breadthHtml(VOLUME.breadth)" in html
        assert "breadth-bar" in html

    def test_overview_line_with_guidance(self):
        html = self._html()
        assert "function ovBreadthLine(b)" in html
        assert "还没有广度快照" in html
        assert "ovBreadthLine(volume.breadth)" in html

    def test_refresh_endpoint_in_nightly(self):
        """「记录到每天」的另一半在 nightly：采集挂在夜间任务里。"""
        nightly = (Path(__file__).parents[1] / "src/quant_research/nightly.py").read_text(
            encoding="utf-8"
        )
        assert "breadth_mod.fetch()" in nightly
        assert 'result["market_breadth"]' in nightly


class TestHoldingsTotals:
    """持仓分析加账户市值汇总 + 个股仓位占比列。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_weight_column_exists(self):
        """个股列表「占比」列：编辑行与正常行都有。"""
        html = self._html()
        assert '"占比"' in html
        # 全文件恰两处：编辑行 + 正常行（切片从定义处起会把自身算进去）
        assert html.count("weightOf(p)") >= 2

    def test_weight_denominator_is_valued_only(self):
        """占比分母 = 该账户**有现价**的市值合计——无现价的票进不了分母
        （不然占比没法定义），汇总里用 N/M 只有行情写明覆盖面。"""
        html = self._html()
        render = html[html.index("const byAccount") : html.index("const rows = visible.map")]
        assert "pos.market_value != null" in render
        assert "只有行情" in html[html.index("const totals") :]

    def test_account_totals_and_grand_total(self):
        html = self._html()
        assert 'id="pf-totals"' in html
        assert "全部合计" in html
        assert "浮动盈亏" in html[html.index("const totals") :]

    def test_unpriced_weight_is_blank_not_zero(self):
        """无现价的票占比留空——不把缺失装成 0%。"""
        html = self._html()
        render = html[html.index("const weightOf") : html.index("const rows = visible.map")]
        assert 'return ""' in render


class TestEscapeAudit:
    """用户手打的账户名/备注进 innerHTML 前必须转义——名字里一个引号
    断掉 value 属性，一个 <img onerror> 就是 XSS。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_account_options_escaped(self):
        """账户下拉（录入 + 筛选）：名字与备注都过 escapeAttr。"""
        html = self._html()
        render = html[html.index("function renderHoldings()") :]
        render = render[: render.index("$(" + '"pf-table"')]
        assert '<option value="${escapeAttr(n)}">${escapeAttr(n)}' in render
        assert "（${escapeAttr(note)}）" in render
        assert '<option value="${escapeAttr(name)}"' in render

    def test_positions_note_and_account_escaped(self):
        """持仓备注（编辑行 + 展示行）与账户名（行首、data-* 属性、占比 title、汇总）转义。"""
        html = self._html()
        render = html[html.index("function renderHoldings()") :]
        render = render[: render.index("$(" + '"pf-table"')]
        assert 'escapeAttr(p.note || "")' in render
        assert "escapeAttr(who)" in render
        assert "escapeAttr(pos.account" in render  # 占比 title
        assert "<b>${escapeAttr(name)}</b>" in render  # 汇总账户名

    def test_render_error_message_escaped(self):
        """错误消息来自后端异常文本，进 innerHTML 前转义。"""
        html = self._html()
        render = html[html.index("function renderError(") :]
        assert (
            'target.innerHTML = `<div class="error-box"><b>加载失败</b>${escapeAttr(message)}'
            in render
        )

    def test_overseas_reason_escaped(self):
        html = self._html()
        assert html.count("${escapeAttr(reason)}") >= 4  # os 页 2 处 + 概览 2 处

    def test_no_bare_quot_only_escape_left(self):
        """旧的只转义引号的写法清零——& < > 同样能破坏属性与上下文。"""
        html = self._html()
        assert '.replace(/"/g, "&quot;")' not in html


class TestReviewProviderSwitch:
    """复盘数据源切换：主源（akshare）被限流补不进数据时，切 baostock
    立即复盘——面板按源隔离，切源不是绕过口径，是用另一份已备好的面板。"""

    @staticmethod
    def _html() -> str:
        return (Path(__file__).parents[1] / "src/quant_research/web/static/index.html").read_text(
            encoding="utf-8"
        )

    def test_provider_select_exists(self):
        html = self._html()
        assert '<select id="pf-provider"' in html
        assert '<option value="baostock">' in html

    def test_load_review_carries_provider(self):
        html = self._html()
        assert (
            'REVIEW = await api(`/portfolio/review${provider ? `?provider=${encodeURIComponent(provider)}` : ""}`);'
            in html
        )

    def test_switch_reloads_review_and_history(self):
        html = self._html()
        assert '$("pf-provider").addEventListener("change"' in html
        assert "loadReviewHistory();" in html  # 复盘历史也跟着源走
