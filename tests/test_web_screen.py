"""Contract tests for the screening panel.

The panel and the ledger are both pointed at fakes, so these cover the contract
— what a route accepts, what it returns, what it refuses to write down — and not
the scoring itself, which has its own tests.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research.data.loader import PricePanel
from quant_research.scoring import screen
from quant_research.scoring.ledger import ScreenLedger
from quant_research.web import api
from quant_research.web.app import create_app

_NAMES = ("600000.SH", "600519.SH", "000001.SZ", "000651.SZ")


def _panel(days: int = 140) -> PricePanel:
    """Four names with distinct trends, so the ranking is not a tie."""
    index = pd.bdate_range("2024-01-01", periods=days, name="date")
    columns = {}
    for position, name in enumerate(_NAMES):
        drift = 0.0002 * (position + 1)
        rng = np.random.default_rng(position)
        columns[name] = 100 * np.cumprod(1 + rng.normal(drift, 0.008, len(index)))
    prices = pd.DataFrame(columns, index=index)
    return PricePanel(prices=prices, extra={"open": prices * 0.999, "amount": prices * 1e5})


def _result(client: TestClient, path: str, **params):
    return client.get(path, params=params)


@pytest.fixture
def client(tmp_path, monkeypatch):
    panel = _panel()
    monkeypatch.setattr(api, "_load_panel", lambda provider="baostock", adjust="qfq": panel)
    monkeypatch.setattr(
        api, "_latest_session", lambda provider, adjust: str(panel.prices.index.max().date())
    )
    monkeypatch.setattr(
        api, "ScreenLedger", lambda *args, **kwargs: ScreenLedger(tmp_path / "screens.jsonl")
    )
    monkeypatch.setattr(api, "load_settings", _settings)
    return TestClient(create_app())


def _settings():
    class _Collection:
        provider = "baostock"
        adjust = "qfq"

    class _Data:
        directory = "data"

    class _Settings:
        collection = _Collection()
        data = _Data()

    return _Settings()


def _finish(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"done", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_the_panel_has_nothing_to_show_before_the_first_run(client):
    response = _result(client, "/api/screen/latest")

    assert response.status_code == 404
    assert "recorded" in response.json()["detail"]


def test_a_screen_job_runs_and_reports_its_picks(client):
    submitted = client.post("/api/jobs/screen", json={"top_n": 3})
    assert submitted.status_code == 202
    assert submitted.json()["kind"] == "screen"

    job = _finish(client, submitted.json()["id"])
    assert job["status"] == "done", job.get("error")

    result = job["result"]
    assert result["as_of"] == result["latest_session"]
    assert result["stale"] is False
    assert len(result["picks"]) <= 3
    assert [pick["rank"] for pick in result["picks"]] == list(range(1, len(result["picks"]) + 1))
    # equal weights across however many names survived
    shares = [pick["weight"] for pick in result["picks"]]
    assert sum(shares) == pytest.approx(1.0, abs=1e-9)


def test_a_run_is_written_down_and_shown_as_the_latest(client):
    job_id = client.post("/api/jobs/screen", json={"top_n": 3}).json()["id"]
    _finish(client, job_id)

    latest = _result(client, "/api/screen/latest")
    assert latest.status_code == 200
    assert latest.json()["as_of"] == str(_panel().prices.index.max().date())
    assert latest.json()["picks"]


def test_a_replay_is_not_written_down(client):
    """Asking about an older day is a question, not an opinion held that day."""
    earlier = str(_panel().prices.index[100].date())
    job_id = client.post("/api/jobs/screen", json={"as_of": earlier, "top_n": 3}).json()["id"]
    job = _finish(client, job_id)

    assert job["status"] == "done", job.get("error")
    assert job["result"]["as_of"] == earlier
    assert job["result"]["stale"] is True  # about an older day, and says so
    assert _result(client, "/api/screen/latest").status_code == 404


def test_record_false_only_shows(client):
    job_id = client.post("/api/jobs/screen", json={"top_n": 3, "record": False}).json()["id"]
    job = _finish(client, job_id)

    assert job["status"] == "done", job.get("error")
    assert job["result"]["picks"]
    assert _result(client, "/api/screen/latest").status_code == 404


def test_review_is_empty_before_any_screen(client):
    payload = _result(client, "/api/screen/review", horizon=5).json()

    assert payload["rows"] == []
    assert payload["screens"] == 0
    assert payload["mean_excess"] is None


def test_review_only_counts_screens_whose_holding_period_has_ended(client, tmp_path):
    """A list made today has not reached its exit yet — so it is not scored yet."""
    job_id = client.post("/api/jobs/screen", json={"top_n": 3}).json()["id"]
    _finish(client, job_id)
    assert _result(client, "/api/screen/review", horizon=5).json()["screens"] == 0

    panel = _panel()
    earlier = str(panel.prices.index[100].date())
    result = screen(panel, as_of=earlier, top_n=3)
    ScreenLedger(tmp_path / "screens.jsonl").record(
        result, provider="baostock", adjust="qfq", params={"top_n": 3}
    )

    payload = _result(client, "/api/screen/review", horizon=5).json()

    assert payload["screens"] == 1
    assert payload["mean_excess"] is not None
    assert payload["beat_market"] is not None
    # a row carries the entry and exit dates, not just a number
    assert payload["rows"][0]["entry"] > earlier
    assert payload["rows"][0]["exit"] > payload["rows"][0]["entry"]


def test_a_source_with_no_data_says_so_rather_than_failing_obscurely(client, monkeypatch):
    def boom(provider: str, adjust: str):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=f"{provider} 还没有落盘数据")

    monkeypatch.setattr(api, "_load_panel", boom)
    job_id = client.post("/api/jobs/screen", json={"top_n": 3}).json()["id"]
    job = _finish(client, job_id)

    assert job["status"] == "failed"
    assert "还没有落盘数据" in job["error"]
