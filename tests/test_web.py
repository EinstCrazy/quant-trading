"""Web API contract tests.

The UI is a shell over the library, so what matters here is the contract: every
number must come from the library and every input error must be a clear 4xx.
Skipped entirely when the ``web`` extra is not installed.
"""

from __future__ import annotations

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
