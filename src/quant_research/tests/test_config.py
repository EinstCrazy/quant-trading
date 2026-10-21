from pathlib import Path

import pytest

from quant_research.config import load_settings


def test_load_settings_from_yaml(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        """
data:
  directory: data
  timezone: UTC
  price_adjustment: adjusted
backtest:
  initial_cash: 100000
  commission_bps: 10
  slippage_bps: 5
  allow_short: false
"""
    )

    settings = load_settings(settings_path)

    assert settings["data"]["directory"] == "data"
    assert settings["backtest"]["initial_cash"] == 100000
    assert settings["backtest"]["allow_short"] is False


def test_load_settings_uses_default_project_file():
    settings = load_settings()

    assert settings["data"]["directory"] == "data"
    assert settings["backtest"]["initial_cash"] > 0


def test_load_settings_rejects_missing_file():
    with pytest.raises(FileNotFoundError):
        load_settings(Path("missing/settings.yaml"))
