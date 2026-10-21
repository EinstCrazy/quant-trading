"""Typed configuration for research defaults.

Settings are loaded from ``config/settings.yaml`` into frozen dataclasses.
Unknown keys raise instead of being silently ignored: a misspelled
``commision_bps`` would otherwise produce a silently cost-free backtest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

PRICE_ADJUSTMENTS = ("adjusted", "forward", "backward", "none")

_DATA_FIELDS = ("directory", "timezone", "price_adjustment")
_BACKTEST_FIELDS = ("initial_cash", "commission_bps", "slippage_bps", "allow_short")


@dataclass(frozen=True)
class DataSettings:
    """Where data comes from and how prices are interpreted."""

    directory: Path
    timezone: str = "UTC"
    price_adjustment: str = "adjusted"

    def __post_init__(self) -> None:
        if self.price_adjustment not in PRICE_ADJUSTMENTS:
            raise ValueError(
                f"data.price_adjustment must be one of {PRICE_ADJUSTMENTS}, "
                f"got {self.price_adjustment!r}"
            )


@dataclass(frozen=True)
class BacktestSettings:
    """Capital and cost assumptions consumed by the backtest engine."""

    initial_cash: float = 100_000.0
    commission_bps: float = 5.0
    slippage_bps: float = 2.0
    allow_short: bool = False

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError(f"backtest.initial_cash must be positive, got {self.initial_cash}")
        if self.commission_bps < 0:
            raise ValueError(
                f"backtest.commission_bps must be non-negative, got {self.commission_bps}"
            )
        if self.slippage_bps < 0:
            raise ValueError(f"backtest.slippage_bps must be non-negative, got {self.slippage_bps}")


@dataclass(frozen=True)
class Settings:
    """Top-level research settings."""

    data: DataSettings
    backtest: BacktestSettings

    def to_dict(self) -> dict[str, Any]:
        """Return a plain nested dict (paths serialised as strings)."""
        payload = asdict(self)
        payload["data"]["directory"] = str(self.data.directory)
        return payload


def _find_default_settings_path() -> Path:
    """Return the project-level settings YAML if it exists."""
    start = Path(__file__).resolve().parent
    for candidate in (start, *start.parents):
        settings_file = candidate / "config" / "settings.yaml"
        if settings_file.is_file():
            return settings_file

    raise FileNotFoundError(
        "Could not find config/settings.yaml in the project tree. "
        "Create the file or pass an explicit path to load_settings()."
    )


def _require_section(config: Mapping[str, Any], name: str, path: Path) -> Mapping[str, Any]:
    if name not in config:
        raise ValueError(f"Settings file is missing required '{name}' section: {path}")
    section = config[name]
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise TypeError(f"Settings section '{name}' must be a mapping: {path}")
    return section


def _reject_unknown(section: Mapping[str, Any], allowed: tuple[str, ...], name: str) -> None:
    unknown = sorted(set(section) - set(allowed))
    if unknown:
        raise ValueError(
            f"Unknown key(s) in '{name}' section: {unknown}. Allowed keys: {list(allowed)}"
        )


def _build_data_settings(section: Mapping[str, Any]) -> DataSettings:
    return DataSettings(
        directory=Path(section.get("directory", "data")),
        timezone=str(section.get("timezone", "UTC")),
        price_adjustment=str(section.get("price_adjustment", "adjusted")),
    )


def _build_backtest_settings(section: Mapping[str, Any]) -> BacktestSettings:
    return BacktestSettings(
        initial_cash=float(section.get("initial_cash", 100_000.0)),
        commission_bps=float(section.get("commission_bps", 5.0)),
        slippage_bps=float(section.get("slippage_bps", 2.0)),
        allow_short=bool(section.get("allow_short", False)),
    )


def load_settings(path: str | Path | None = None) -> Settings:
    """Load research settings from a YAML file into frozen dataclasses.

    When no path is provided, this resolves the project-level ``config/settings.yaml``
    by walking up from the package directory. Unknown keys and invalid values raise
    ``ValueError`` so that a typo can never turn into a silently wrong assumption.
    """
    settings_path = Path(path) if path is not None else _find_default_settings_path()
    settings_path = settings_path.expanduser().resolve()

    if not settings_path.is_file():
        raise FileNotFoundError(f"Settings file not found: {settings_path}")

    with settings_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    if not isinstance(config, Mapping):
        raise TypeError(f"Settings file must contain a mapping at the top level: {settings_path}")

    data_section = _require_section(config, "data", settings_path)
    backtest_section = _require_section(config, "backtest", settings_path)
    _reject_unknown(data_section, _DATA_FIELDS, "data")
    _reject_unknown(backtest_section, _BACKTEST_FIELDS, "backtest")

    return Settings(
        data=_build_data_settings(data_section),
        backtest=_build_backtest_settings(backtest_section),
    )


__all__ = ["BacktestSettings", "DataSettings", "Settings", "load_settings"]
