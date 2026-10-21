from pathlib import Path

import pytest

from quant_research.config import load_settings

MINIMAL_SETTINGS = """
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


def test_load_settings_from_yaml(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(MINIMAL_SETTINGS)

    settings = load_settings(settings_path)

    assert settings.data.directory == Path("data")
    assert settings.data.timezone == "UTC"
    assert settings.backtest.initial_cash == 100000
    assert settings.backtest.commission_bps == 10
    assert settings.backtest.allow_short is False


def test_load_settings_uses_default_project_file():
    settings = load_settings()

    assert settings.data.directory
    assert settings.backtest.initial_cash > 0


def test_load_settings_rejects_missing_file():
    with pytest.raises(FileNotFoundError):
        load_settings(Path("missing/settings.yaml"))


def test_load_settings_rejects_unknown_key(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(MINIMAL_SETTINGS + "  commision_bps: 1\n")

    with pytest.raises(ValueError, match="Unknown key"):
        load_settings(settings_path)


def test_load_settings_rejects_missing_section(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("data:\n  directory: data\n")

    with pytest.raises(ValueError, match="missing required 'backtest'"):
        load_settings(settings_path)


def test_load_settings_rejects_invalid_values(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        """
data:
  directory: data
  price_adjustment: sideways
backtest:
  initial_cash: 0
"""
    )

    with pytest.raises(ValueError, match="price_adjustment"):
        load_settings(settings_path)


def test_settings_are_immutable(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(MINIMAL_SETTINGS)

    settings = load_settings(settings_path)

    with pytest.raises(AttributeError):
        settings.backtest.initial_cash = 1  # type: ignore[misc]


def test_settings_to_dict(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(MINIMAL_SETTINGS)

    payload = load_settings(settings_path).to_dict()

    assert payload["data"]["directory"] == "data"
    assert payload["backtest"]["slippage_bps"] == 5
