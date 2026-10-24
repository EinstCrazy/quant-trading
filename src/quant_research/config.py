"""Typed configuration for research defaults.

Settings are loaded from ``config/settings.yaml`` into frozen dataclasses.
Unknown keys raise instead of being silently ignored: a misspelled
``commision_bps`` would otherwise produce a silently cost-free backtest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

import yaml

PRICE_ADJUSTMENTS = ("adjusted", "forward", "backward", "none")
COLLECTION_ADJUSTMENTS = ("qfq", "hfq", "none")
STORAGE_FORMATS = ("parquet", "csv")

_DATA_FIELDS = ("directory", "timezone", "price_adjustment")
_BACKTEST_FIELDS = ("initial_cash", "commission_bps", "slippage_bps", "allow_short")
_COLLECTION_FIELDS = (
    "provider",
    "symbols",
    "output_directory",
    "format",
    "adjust",
    "lookback_days",
    "schedule_at",
    "timezone",
    "retries",
    "pause_seconds",
    "report_directory",
)


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
class CollectionSettings:
    """How the daily data collector runs.

    ``output_directory`` is deliberately separate from ``data.directory``: the
    collector writes raw, as-received files, while research code reads whatever
    layout it is pointed at.
    """

    provider: str = "akshare"
    symbols: tuple[str, ...] = ()
    output_directory: Path = Path("data/raw")
    format: str = "parquet"
    adjust: str = "qfq"
    lookback_days: int = 10
    schedule_at: str = "18:30"
    timezone: str = "Asia/Shanghai"
    retries: int = 2
    pause_seconds: float = 0.5
    report_directory: Path = Path("reports/collection")

    def __post_init__(self) -> None:
        if self.format not in STORAGE_FORMATS:
            raise ValueError(
                f"collection.format must be one of {STORAGE_FORMATS}, got {self.format!r}"
            )
        if self.adjust not in COLLECTION_ADJUSTMENTS:
            raise ValueError(
                f"collection.adjust must be one of {COLLECTION_ADJUSTMENTS}, got {self.adjust!r}"
            )
        if self.lookback_days < 0:
            raise ValueError(
                f"collection.lookback_days must be non-negative, got {self.lookback_days}"
            )
        if self.retries < 0:
            raise ValueError(f"collection.retries must be non-negative, got {self.retries}")
        _parse_clock(self.schedule_at)

    def at_time(self) -> time:
        """Return ``schedule_at`` as a ``datetime.time``."""
        return _parse_clock(self.schedule_at)


def _parse_clock(text: str) -> time:
    try:
        hour, minute = (int(part) for part in str(text).split(":", maxsplit=1))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"collection.schedule_at must look like 'HH:MM', got {text!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Top-level research settings."""

    data: DataSettings
    backtest: BacktestSettings
    collection: CollectionSettings = field(default_factory=CollectionSettings)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain nested dict (paths serialised as strings)."""
        payload = asdict(self)
        payload["data"]["directory"] = str(self.data.directory)
        payload["collection"]["output_directory"] = str(self.collection.output_directory)
        payload["collection"]["report_directory"] = str(self.collection.report_directory)
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


def _build_collection_settings(section: Mapping[str, Any]) -> CollectionSettings:
    defaults = CollectionSettings()
    symbols = section.get("symbols") or ()
    if isinstance(symbols, str):
        symbols = tuple(part.strip() for part in symbols.split(",") if part.strip())
    return CollectionSettings(
        provider=str(section.get("provider", defaults.provider)),
        symbols=tuple(str(item) for item in symbols),
        output_directory=Path(section.get("output_directory", defaults.output_directory)),
        format=str(section.get("format", defaults.format)),
        adjust=str(section.get("adjust", defaults.adjust)),
        lookback_days=int(section.get("lookback_days", defaults.lookback_days)),
        schedule_at=str(section.get("schedule_at", defaults.schedule_at)),
        timezone=str(section.get("timezone", defaults.timezone)),
        retries=int(section.get("retries", defaults.retries)),
        pause_seconds=float(section.get("pause_seconds", defaults.pause_seconds)),
        report_directory=Path(section.get("report_directory", defaults.report_directory)),
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
    # ``collection`` is optional so existing settings files keep working.
    collection_section = config.get("collection") or {}
    if not isinstance(collection_section, Mapping):
        raise TypeError(f"Settings section 'collection' must be a mapping: {settings_path}")

    _reject_unknown(data_section, _DATA_FIELDS, "data")
    _reject_unknown(backtest_section, _BACKTEST_FIELDS, "backtest")
    _reject_unknown(collection_section, _COLLECTION_FIELDS, "collection")

    return Settings(
        data=_build_data_settings(data_section),
        backtest=_build_backtest_settings(backtest_section),
        collection=_build_collection_settings(collection_section),
    )


__all__ = [
    "COLLECTION_ADJUSTMENTS",
    "STORAGE_FORMATS",
    "BacktestSettings",
    "CollectionSettings",
    "DataSettings",
    "Settings",
    "load_settings",
]
