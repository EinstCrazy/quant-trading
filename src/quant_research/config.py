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
    "provider_options",
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
    # extra kwargs handed to the provider constructor — futu's host/port/markets
    # live here, because no other source needs them and a field per source is
    # how config classes rot
    provider_options: dict[str, Any] = field(default_factory=dict)

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


_INDICATOR_FIELDS = (
    "output_directory",
    "format",
    "ma_periods",
    "ema_periods",
    "macd_fast",
    "macd_slow",
    "macd_signal",
    "rsi_period",
    "boll_period",
    "boll_std",
    "kdj_period",
    "kdj_smooth",
    "atr_period",
    "williams_period",
    "cci_period",
    "adx_period",
    "volatility_period",
    "bias_period",
    "roc_period",
    "psy_period",
    "mfi_period",
    "volume_ratio_period",
    "donchian_period",
    "vr_period",
    "turnover_ma_period",
)


@dataclass(frozen=True)
class IndicatorSettings:
    """Parameters for the technical indicators, with the usual defaults.

    Periods are validated here rather than at the point of use: a typo that says
    ``rsi_period: 0`` should refuse to start, not hand ``ewm`` a zero window.
    """

    output_directory: Path = Path("data/indicators")
    format: str = "parquet"
    ma_periods: tuple[int, ...] = (5, 10, 20, 60)
    ema_periods: tuple[int, ...] = (12, 26)
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    boll_period: int = 20
    boll_std: float = 2.0
    kdj_period: int = 9
    kdj_smooth: int = 3
    atr_period: int = 14
    williams_period: int = 14
    cci_period: int = 14
    adx_period: int = 14
    volatility_period: int = 20
    bias_period: int = 20
    roc_period: int = 12
    psy_period: int = 12
    mfi_period: int = 14
    volume_ratio_period: int = 5
    donchian_period: int = 20
    vr_period: int = 26
    turnover_ma_period: int = 20

    def __post_init__(self) -> None:
        if self.format not in STORAGE_FORMATS:
            raise ValueError(
                f"indicators.format must be one of {STORAGE_FORMATS}, got {self.format!r}"
            )
        periods = {
            "macd_fast": self.macd_fast,
            "macd_slow": self.macd_slow,
            "macd_signal": self.macd_signal,
            "rsi_period": self.rsi_period,
            "boll_period": self.boll_period,
            "kdj_period": self.kdj_period,
            "kdj_smooth": self.kdj_smooth,
            "atr_period": self.atr_period,
            "williams_period": self.williams_period,
            "cci_period": self.cci_period,
            "adx_period": self.adx_period,
            "volatility_period": self.volatility_period,
            "bias_period": self.bias_period,
            "roc_period": self.roc_period,
            "psy_period": self.psy_period,
            "mfi_period": self.mfi_period,
            "volume_ratio_period": self.volume_ratio_period,
            "donchian_period": self.donchian_period,
            "vr_period": self.vr_period,
            "turnover_ma_period": self.turnover_ma_period,
        }
        for name, value in periods.items():
            if value < 1:
                raise ValueError(f"indicators.{name} must be at least 1, got {value}")
        for name in ("ma_periods", "ema_periods"):
            for period in getattr(self, name):
                if period < 1:
                    raise ValueError(f"indicators.{name} entries must be at least 1, got {period}")
        if self.boll_std <= 0:
            raise ValueError(f"indicators.boll_std must be positive, got {self.boll_std}")


_FUNDAMENTALS_FIELDS = ("output_directory", "quarters", "pause_seconds")


@dataclass(frozen=True)
class FundamentalsSettings:
    """How much financial history to keep, and where.

    ``quarters`` defaults to eight: two years shows a trend, while asking for ten
    years of filings multiplies the round trips by five to answer a question the
    last two already answered.
    """

    output_directory: Path = Path("data/fundamentals")
    quarters: int = 8
    pause_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.quarters < 1:
            raise ValueError(f"fundamentals.quarters must be at least 1, got {self.quarters}")
        if self.pause_seconds < 0:
            raise ValueError(
                f"fundamentals.pause_seconds cannot be negative, got {self.pause_seconds}"
            )


_REGULATORY_FIELDS = (
    "output_directory",
    "lookback_days",
    "active_days",
    "manual_file",
    "pause_seconds",
    "page_size",
    "max_pages",
)


@dataclass(frozen=True)
class RegulatorySettings:
    """Where the regulatory-event store lives, and how far back to search.

    ``lookback_days`` is a year because a case can stay open that long; a
    shorter window silently clears the companies put under investigation long
    ago and never closed. ``active_days`` is how long an unclosed 立案 keeps a
    name out of the screen — the same year, for the same reason.
    """

    output_directory: Path = Path("data/regulatory")
    lookback_days: int = 365
    active_days: int = 365
    manual_file: Path = Path("config/regulatory.yaml")
    pause_seconds: float = 0.2
    page_size: int = 50
    max_pages: int = 40

    def __post_init__(self) -> None:
        if self.lookback_days < 1:
            raise ValueError(
                f"regulatory.lookback_days must be at least 1, got {self.lookback_days}"
            )
        if self.active_days < 1:
            raise ValueError(f"regulatory.active_days must be at least 1, got {self.active_days}")
        if self.page_size < 1:
            raise ValueError(f"regulatory.page_size must be at least 1, got {self.page_size}")
        if self.max_pages < 1:
            raise ValueError(f"regulatory.max_pages must be at least 1, got {self.max_pages}")


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
    indicators: IndicatorSettings = field(default_factory=IndicatorSettings)
    fundamentals: FundamentalsSettings = field(default_factory=FundamentalsSettings)
    regulatory: RegulatorySettings = field(default_factory=RegulatorySettings)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain nested dict (paths serialised as strings)."""
        payload = asdict(self)
        payload["data"]["directory"] = str(self.data.directory)
        payload["collection"]["output_directory"] = str(self.collection.output_directory)
        payload["collection"]["report_directory"] = str(self.collection.report_directory)
        payload["indicators"]["output_directory"] = str(self.indicators.output_directory)
        payload["indicators"]["ma_periods"] = list(self.indicators.ma_periods)
        payload["indicators"]["ema_periods"] = list(self.indicators.ema_periods)
        payload["fundamentals"]["output_directory"] = str(self.fundamentals.output_directory)
        payload["regulatory"]["output_directory"] = str(self.regulatory.output_directory)
        payload["regulatory"]["manual_file"] = str(self.regulatory.manual_file)
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
        provider_options=dict(section.get("provider_options", defaults.provider_options) or {}),
    )


def _periods(section: Mapping[str, Any], key: str, fallback: tuple[int, ...]) -> tuple[int, ...]:
    raw = section.get(key)
    if raw is None:
        return fallback
    if isinstance(raw, (int, float)):
        return (int(raw),)
    return tuple(int(item) for item in raw)


def _build_indicator_settings(section: Mapping[str, Any]) -> IndicatorSettings:
    defaults = IndicatorSettings()
    return IndicatorSettings(
        output_directory=Path(section.get("output_directory", defaults.output_directory)),
        format=str(section.get("format", defaults.format)),
        ma_periods=_periods(section, "ma_periods", defaults.ma_periods),
        ema_periods=_periods(section, "ema_periods", defaults.ema_periods),
        macd_fast=int(section.get("macd_fast", defaults.macd_fast)),
        macd_slow=int(section.get("macd_slow", defaults.macd_slow)),
        macd_signal=int(section.get("macd_signal", defaults.macd_signal)),
        rsi_period=int(section.get("rsi_period", defaults.rsi_period)),
        boll_period=int(section.get("boll_period", defaults.boll_period)),
        boll_std=float(section.get("boll_std", defaults.boll_std)),
        kdj_period=int(section.get("kdj_period", defaults.kdj_period)),
        kdj_smooth=int(section.get("kdj_smooth", defaults.kdj_smooth)),
        atr_period=int(section.get("atr_period", defaults.atr_period)),
        williams_period=int(section.get("williams_period", defaults.williams_period)),
        cci_period=int(section.get("cci_period", defaults.cci_period)),
        adx_period=int(section.get("adx_period", defaults.adx_period)),
        volatility_period=int(section.get("volatility_period", defaults.volatility_period)),
    )


def _build_fundamentals_settings(section: Mapping[str, Any]) -> FundamentalsSettings:
    defaults = FundamentalsSettings()
    return FundamentalsSettings(
        output_directory=Path(section.get("output_directory", defaults.output_directory)),
        quarters=int(section.get("quarters", defaults.quarters)),
        pause_seconds=float(section.get("pause_seconds", defaults.pause_seconds)),
    )


def _build_regulatory_settings(section: Mapping[str, Any]) -> RegulatorySettings:
    defaults = RegulatorySettings()
    return RegulatorySettings(
        output_directory=Path(section.get("output_directory", defaults.output_directory)),
        lookback_days=int(section.get("lookback_days", defaults.lookback_days)),
        active_days=int(section.get("active_days", defaults.active_days)),
        manual_file=Path(section.get("manual_file", defaults.manual_file)),
        pause_seconds=float(section.get("pause_seconds", defaults.pause_seconds)),
        page_size=int(section.get("page_size", defaults.page_size)),
        max_pages=int(section.get("max_pages", defaults.max_pages)),
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
    # ``collection`` and ``indicators`` are optional so existing settings files
    # keep working.
    collection_section = config.get("collection") or {}
    if not isinstance(collection_section, Mapping):
        raise TypeError(f"Settings section 'collection' must be a mapping: {settings_path}")
    indicator_section = config.get("indicators") or {}
    if not isinstance(indicator_section, Mapping):
        raise TypeError(f"Settings section 'indicators' must be a mapping: {settings_path}")
    fundamental_section = config.get("fundamentals") or {}
    if not isinstance(fundamental_section, Mapping):
        raise TypeError(f"Settings section 'fundamentals' must be a mapping: {settings_path}")
    regulatory_section = config.get("regulatory") or {}
    if not isinstance(regulatory_section, Mapping):
        raise TypeError(f"Settings section 'regulatory' must be a mapping: {settings_path}")

    _reject_unknown(data_section, _DATA_FIELDS, "data")
    _reject_unknown(backtest_section, _BACKTEST_FIELDS, "backtest")
    _reject_unknown(collection_section, _COLLECTION_FIELDS, "collection")
    _reject_unknown(indicator_section, _INDICATOR_FIELDS, "indicators")
    _reject_unknown(fundamental_section, _FUNDAMENTALS_FIELDS, "fundamentals")
    _reject_unknown(regulatory_section, _REGULATORY_FIELDS, "regulatory")

    return Settings(
        data=_build_data_settings(data_section),
        backtest=_build_backtest_settings(backtest_section),
        collection=_build_collection_settings(collection_section),
        indicators=_build_indicator_settings(indicator_section),
        fundamentals=_build_fundamentals_settings(fundamental_section),
        regulatory=_build_regulatory_settings(regulatory_section),
    )


__all__ = [
    "COLLECTION_ADJUSTMENTS",
    "STORAGE_FORMATS",
    "BacktestSettings",
    "CollectionSettings",
    "DataSettings",
    "FundamentalsSettings",
    "IndicatorSettings",
    "RegulatorySettings",
    "Settings",
    "load_settings",
]
