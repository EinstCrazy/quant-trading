"""Tests for the technical indicators.

The formulas are checked against hand-computed numbers, not against another
implementation — an implementation that shares a mistake with this one would
agree with it all the way down.

The property that matters most is the last one: truncating the history and
recomputing must give the same numbers for the days that overlap. That is what
"no looking into the future" means in practice.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_research.config import IndicatorSettings
from quant_research.data.loader import PricePanel
from quant_research.indicators import (
    INDICATOR_CATALOG,
    IndicatorParams,
    adx,
    atr,
    boll,
    catalog,
    cci,
    compute_all,
    doc_for,
    ema,
    kdj,
    load_bundle,
    macd,
    obv,
    panel_from_frames,
    params_from_settings,
    realized_volatility,
    rsi,
    sma,
    store_bundle,
    true_range,
    vwap,
    williams_r,
)

_DATES = pd.bdate_range("2024-01-01", periods=40, name="date")


def _frame(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"A.SH": values}, index=_DATES[: len(values)])


def _panel(days: int = 40, names: tuple[str, ...] = ("A.SH", "B.SH")) -> PricePanel:
    """Two names with distinct trends, all six fields present."""
    rng = np.random.default_rng(7)
    fields: dict[str, pd.DataFrame] = {}
    columns = {}
    for position, name in enumerate(names):
        drift = 0.0005 * (position + 1)
        close = 100 * np.cumprod(1 + rng.normal(drift, 0.008, days))
        columns[name] = close
    index = pd.bdate_range("2024-01-01", periods=days, name="date")
    close = pd.DataFrame(columns, index=index)
    fields["close"] = close
    fields["open"] = close.shift(1).fillna(close.iloc[0])
    fields["high"] = close * 1.004
    fields["low"] = close * 0.996
    fields["volume"] = pd.DataFrame(1_000.0, index=index, columns=list(names))
    fields["amount"] = close * 1_000 * 100
    return PricePanel(prices=close, extra={k: v for k, v in fields.items() if k != "close"})


# --- hand-checked formulas -------------------------------------------------


def test_ema_follows_the_recursive_definition():
    """E_t = 2/(n+1)·x_t + (1 - 2/(n+1))·E_{t-1}, worked out by hand."""
    result = ema(_frame([1.0, 2.0, 3.0, 4.0]), n=2)["A.SH"]

    # n=2 and min_periods=2, so the first bar has no EMA yet
    assert result.iloc[0:1].isna().all()
    assert result.iloc[1] == pytest.approx(2 / 3 * 2 + 1 / 3 * 1)  # 1.6667
    assert result.iloc[2] == pytest.approx(2 / 3 * 3 + 1 / 3 * (5 / 3))  # 2.5556
    assert result.iloc[3] == pytest.approx(2 / 3 * 4 + 1 / 3 * (23 / 9))  # 3.5185
    # 1.6667 rather than 1.75: the pandas default (adjust=True) reweights the warm-up


def test_ema_stays_nan_until_the_window_is_full():
    result = ema(_frame([1.0, 2.0, 3.0, 4.0, 5.0]), n=3)

    assert result["A.SH"].iloc[:2].isna().all()  # no 3-day EMA from one bar
    assert result["A.SH"].iloc[2:].notna().all()


def test_sma_is_the_plain_mean():
    result = sma(_frame([1.0, 2.0, 3.0, 4.0]), n=2)

    assert result["A.SH"].iloc[0:1].isna().all()
    assert result["A.SH"].iloc[1] == pytest.approx(1.5)
    assert result["A.SH"].iloc[3] == pytest.approx(3.5)


def test_rsi_matches_a_worked_example():
    """close 10→11→10→12 with n=2, Wilder smoothing."""
    close = _frame([10.0, 11.0, 10.0, 12.0])
    result = rsi(close, n=2)["A.SH"]

    # gains [NaN, 1, 0, 2], losses [NaN, 0, 1, 0], alpha = 1/2.
    # ewm skips the leading NaN, so the first average is the first observation:
    #   avg_gain: 1, 0.5, 1.25        avg_loss: 0, 0.5, 0.25
    assert result.iloc[2] == pytest.approx(100 - 100 / (1 + 0.5 / 0.5))  # 50
    assert result.iloc[3] == pytest.approx(100 - 100 / (1 + 1.25 / 0.25))  # 83.33
    assert result.iloc[0:2].isna().to_numpy().all()


def test_rsi_is_100_when_nothing_fell_and_nan_when_nothing_moved():
    rising = _frame([1.0, 2.0, 3.0, 4.0])
    assert rsi(rising, n=2)["A.SH"].iloc[3] == pytest.approx(100.0)

    flat = _frame([5.0, 5.0, 5.0, 5.0])
    # a window with no gains and no losses carries no information; 100 would
    # invent a signal that is not there
    assert rsi(flat, n=2)["A.SH"].isna().all()


def test_macd_histogram_uses_the_doubled_convention():
    close = pd.DataFrame(
        {"A.SH": 100 * np.cumprod(1 + np.random.default_rng(3).normal(0, 0.01, 40))},
        index=_DATES,
    )
    lines = macd(close, 12, 26, 9)

    assert np.allclose(
        lines.hist.to_numpy(dtype=float),
        (2 * (lines.dif - lines.dea)).to_numpy(dtype=float),
        equal_nan=True,
    )
    assert lines.dif.iloc[:25].isna().to_numpy().all()  # EMA(26) needs 26 bars


def test_boll_uses_the_population_standard_deviation():
    """A flat series has no spread at all, so the band collapses onto the mean."""
    result = boll(_frame([10.0, 10.0, 10.0, 10.0]), n=4, k=2.0)

    assert result.mid["A.SH"].iloc[3] == pytest.approx(10.0)
    assert result.upper["A.SH"].iloc[3] == pytest.approx(10.0)
    assert result.width["A.SH"].iloc[3] == pytest.approx(0.0)

    spread = boll(_frame([1.0, 2.0, 3.0, 4.0, 5.0]), n=5, k=2.0)
    # population std of 1..5 is sqrt(2); ddof=1 would give sqrt(2.5)
    assert spread.upper["A.SH"].iloc[4] == pytest.approx(3 + 2 * np.sqrt(2))


def test_true_range_spans_the_gap():
    high = _frame([11.0, 12.0, 13.0])
    low = _frame([9.0, 10.0, 11.0])
    close = _frame([10.0, 11.0, 12.0])

    result = true_range(high, low, close)["A.SH"]

    assert result.iloc[0] == pytest.approx(2.0)  # no previous close on day one
    assert result.iloc[2] == pytest.approx(2.0)

    # a gap down: previous close 12, then a day that trades 5..13 — the span is 8,
    # wider than the |high - prev close| of 1, and that is the whole point of TR
    gapped = true_range(_frame([13.0, 13.0]), _frame([11.0, 5.0]), _frame([12.0, 6.0]))
    assert gapped["A.SH"].iloc[1] == pytest.approx(8.0)


def test_atr_is_the_smoothed_true_range():
    high, low, close = (
        _frame([11.0, 12.0, 13.0]),
        _frame([9.0, 10.0, 11.0]),
        _frame([10.0, 11.0, 12.0]),
    )

    result = atr(high, low, close, n=2)["A.SH"]

    assert result.iloc[:1].isna().all()
    assert result.iloc[1] == pytest.approx(2.0)
    assert result.iloc[2] == pytest.approx(2.0)


def test_kdj_matches_a_worked_example():
    """n=3, so RSV starts on the third bar; K and D start at 50."""
    close = _frame([10.0, 11.0, 12.0, 13.0, 14.0])
    high = close + 1
    low = close - 1

    result = kdj(high, low, close, n=3, m1=3, m2=3)
    k, d, j = result.k["A.SH"], result.d["A.SH"], result.j["A.SH"]

    # RSV on bars 3 and 4: (12-9)/(13-9)*100 = 75, (13-10)/(14-10)*100 = 75
    assert k.iloc[:2].isna().all()
    assert k.iloc[2] == pytest.approx(2 / 3 * 50 + 1 / 3 * 75)
    assert d.iloc[2] == pytest.approx(2 / 3 * 50 + 1 / 3 * k.iloc[2])
    assert k.iloc[3] == pytest.approx(2 / 3 * k.iloc[2] + 1 / 3 * 75)
    assert j.iloc[2] == pytest.approx(3 * k.iloc[2] - 2 * d.iloc[2])


def test_kdj_holds_through_a_day_with_no_range():
    """A limit-up lock makes high == low: RSV is undefined, so carry the value.

    Letting NaN into the recursion would blank the rest of the series; inventing
    an RSV of 0 or 100 would invent a signal. Holding is the honest answer.
    """
    index = _DATES[:5]
    close = pd.DataFrame({"A.SH": [10.0, 11.0, 12.0, 12.0, 12.0]}, index=index)
    high = pd.DataFrame({"A.SH": [11.0, 12.0, 13.0, 12.0, 12.0]}, index=index)
    low = pd.DataFrame({"A.SH": [9.0, 10.0, 11.0, 12.0, 12.0]}, index=index)
    # days 4 and 5 are both locked at 12, so day 5's whole window has no range

    result = kdj(high, low, close, n=2, m1=3, m2=3)
    k = result.k["A.SH"]

    assert k.iloc[1:].notna().all()  # only the warm-up bar is NaN
    assert k.iloc[4] == pytest.approx(k.iloc[3])  # carried, not invented
    # the range-based indicator has no answer for that day, and says so
    assert pd.isna(williams_r(high, low, close, n=2)["A.SH"].iloc[4])


def test_williams_r_locates_the_close_within_the_range():
    high = _frame([11.0, 12.0, 13.0])
    low = _frame([9.0, 10.0, 11.0])
    close = _frame([10.0, 11.0, 12.0])

    result = williams_r(high, low, close, n=2)["A.SH"]

    assert result.iloc[1] == pytest.approx((12 - 11) / (12 - 9) * -100)


def test_cci_is_nan_when_nothing_deviates():
    """Mean absolute deviation of zero has no answer; 0 would be a lie."""
    flat = _frame([10.0, 10.0, 10.0, 10.0])

    assert cci(flat, flat, flat, n=4)["A.SH"].isna().all()


def test_obv_accumulates_signed_volume():
    close = _frame([10.0, 11.0, 10.0])
    volume = _frame([100.0, 200.0, 300.0])

    result = obv(close, volume)["A.SH"]

    assert result.iloc[0] == pytest.approx(0.0)
    assert result.iloc[1] == pytest.approx(200.0)
    assert result.iloc[2] == pytest.approx(-100.0)


def test_vwap_converts_lots_to_shares():
    """volume is in lots, amount in yuan — hence the 100."""
    amount = _frame([1_000_000.0])
    volume = _frame([100.0])

    assert vwap(amount, volume)["A.SH"].iloc[0] == pytest.approx(100.0)
    assert vwap(amount, _frame([0.0]))["A.SH"].isna().all()


def test_adx_stays_nan_while_the_window_fills():
    panel = _panel(days=30)
    result = adx(panel.field("high"), panel.field("low"), panel.prices, n=14)

    assert result.adx.iloc[:14].isna().to_numpy().all()
    assert result.plus_di.dropna(how="all").shape[0] > 0


def test_realized_volatility_annualises_with_the_sample_std():
    flat = _frame([10.0, 10.0, 10.0, 10.0])

    assert realized_volatility(flat, n=3)["A.SH"].dropna().eq(0.0).all()


# --- the whole bundle ------------------------------------------------------


def test_compute_all_covers_every_field_the_panel_has():
    bundle = compute_all(_panel(days=40))

    for name in ("ma_5", "ma_20", "ema_12", "macd_dif", "macd_dea", "macd_hist", "rsi_14"):
        assert name in bundle.frames
    for name in ("boll_mid", "boll_upper", "boll_lower", "boll_width", "volatility_20"):
        assert name in bundle.frames
    for name in ("kdj_k", "kdj_d", "kdj_j", "atr_14", "wr_14", "cci_14", "adx_14"):
        assert name in bundle.frames
    for name in ("obv", "vwap"):
        assert name in bundle.frames

    for frame in bundle.frames.values():
        assert list(frame.index) == list(_panel(days=40).prices.index)
        assert list(frame.columns) == list(_panel(days=40).prices.columns)


def test_compute_all_degrades_to_close_only_indicators():
    """A CSV of closes has no high/low/volume; it should still get its indicators."""
    close_only = PricePanel(prices=_panel(days=40).prices)

    bundle = compute_all(close_only)

    assert "rsi_14" in bundle.frames
    assert "boll_upper" in bundle.frames
    assert "macd_hist" in bundle.frames
    # and none of the ones that need fields this panel does not carry
    assert "kdj_k" not in bundle.frames
    assert "atr_14" not in bundle.frames
    assert "obv" not in bundle.frames
    assert "vwap" not in bundle.frames


def test_the_key_carries_the_period_so_settings_can_never_be_confused():
    bundle = compute_all(_panel(days=40), IndicatorParams(rsi_period=21, boll_period=30))

    assert "rsi_21" in bundle.frames
    assert "rsi_14" not in bundle.frames
    assert "boll_upper" in bundle.frames  # the band keeps its name, the JSON records the period


def test_indicators_do_not_look_into_the_future():
    """Truncating the history must not change a single overlapping value."""
    panel = _panel(days=60)
    full = compute_all(panel)

    cut = 40
    truncated_panel = PricePanel(
        prices=panel.prices.iloc[:cut],
        extra={name: frame.iloc[:cut] for name, frame in panel.extra.items()},
    )
    truncated = compute_all(truncated_panel)

    for name, frame in full.frames.items():
        shared = frame.iloc[:cut]
        assert np.allclose(
            shared.to_numpy(dtype=float),
            truncated.frames[name].to_numpy(dtype=float),
            equal_nan=True,
        ), name


# --- parameters and config -------------------------------------------------


def test_params_reject_periods_that_cannot_work():
    with pytest.raises(ValueError, match="rsi_period"):
        IndicatorParams(rsi_period=0)
    with pytest.raises(ValueError, match="boll_std"):
        IndicatorParams(boll_std=0)
    with pytest.raises(ValueError, match="ma_periods"):
        IndicatorParams(ma_periods=(0, 5))


def test_settings_reject_the_same_mistakes():
    with pytest.raises(ValueError, match="rsi_period"):
        IndicatorSettings(rsi_period=0)
    with pytest.raises(ValueError, match="format"):
        IndicatorSettings(format="xlsx")


def test_params_follow_the_loaded_settings(tmp_path):
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(
        "data:\n  directory: data\nbacktest: {}\nindicators:\n  rsi_period: 21\n"
        "  boll_std: 2.5\n  ma_periods: [10, 30]\n  output_directory: out/ind\n",
        encoding="utf-8",
    )
    from quant_research.config import load_settings

    params = params_from_settings(load_settings(settings_file))

    assert params.rsi_period == 21
    assert params.boll_std == 2.5
    assert params.ma_periods == (10, 30)
    assert params.ema_periods == (12, 26)  # everything else falls back to convention


def test_the_project_settings_carry_an_indicators_section():
    from quant_research.config import load_settings

    params = params_from_settings(load_settings())

    assert params.rsi_period == 14
    assert params.ma_periods == (5, 10, 20, 60)


# --- storage ---------------------------------------------------------------


def test_a_stored_bundle_round_trips(tmp_path):
    bundle = compute_all(_panel(days=30))

    target = store_bundle(bundle, tmp_path / "indicators", "baostock", "qfq")
    loaded = load_bundle(tmp_path / "indicators", "baostock", "qfq")

    assert target.is_dir()
    assert loaded is not None
    assert loaded.fields == bundle.fields
    assert loaded.params == bundle.params
    for name in bundle.frames:
        assert np.allclose(
            loaded.frames[name].to_numpy(dtype=float),
            bundle.frames[name].to_numpy(dtype=float),
            equal_nan=True,
        )
    assert loaded.meta["symbols"] == 2
    assert loaded.meta["params"]["rsi_period"] == 14


def test_a_stored_bundle_can_be_stored_as_csv(tmp_path):
    bundle = compute_all(_panel(days=25))

    store_bundle(bundle, tmp_path, "mock", "none", fmt="csv")
    loaded = load_bundle(tmp_path, "mock", "none")

    assert loaded is not None
    assert "rsi_14" in loaded.frames


def test_asking_for_a_bundle_that_was_never_stored_gives_none(tmp_path):
    assert load_bundle(tmp_path, "baostock", "qfq") is None


# --- panel assembly --------------------------------------------------------


def test_panel_from_frames_stacks_every_field_a_source_carries():
    index = pd.bdate_range("2024-01-01", periods=5, name="date")
    frames = {
        "600000.SH": pd.DataFrame(
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 100.0, "amount": 1e5},
            index=index,
        ),
        "000001.SZ": pd.DataFrame(
            {
                "open": 20.0,
                "high": 21.0,
                "low": 19.0,
                "close": 20.0,
                "volume": 200.0,
                "amount": 2e5,
            },
            index=index,
        ),
    }

    panel = panel_from_frames(frames, source="baostock", adjust="qfq")

    assert list(panel.prices.columns) == ["600000.SH", "000001.SZ"]
    assert panel.has_field("high") and panel.has_field("volume") and panel.has_field("amount")
    assert panel.prices.loc[index[0], "000001.SZ"] == pytest.approx(20.0)
    assert panel.meta.n_symbols == 2


def test_panel_from_frames_degrades_to_close_only():
    index = pd.bdate_range("2024-01-01", periods=3, name="date")
    frames = {"600000.SH": pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=index)}

    panel = panel_from_frames(frames)

    assert panel.has_field("close")
    assert not panel.has_field("volume")
    assert compute_all(panel).fields[0] == "ma_5"


def test_panel_from_frames_refuses_a_source_without_closes():
    index = pd.bdate_range("2024-01-01", periods=3, name="date")

    with pytest.raises(ValueError, match="close"):
        panel_from_frames({"600000.SH": pd.DataFrame({"volume": [1.0, 2.0, 3.0]}, index=index)})


# --- the catalogue an interface reads --------------------------------------


def test_every_computed_field_can_be_explained():
    """A number on a screen that cannot be explained is worse than no number."""
    bundle = compute_all(_panel(days=40))

    missing = [field for field in bundle.fields if doc_for(field) is None]
    assert missing == []


def test_the_catalogue_carries_the_text_an_interface_needs():
    entry = catalog()["rsi"]

    assert entry["label"].startswith("RSI")
    assert entry["unit"] == "0–100"
    assert entry["typical_range"]
    assert entry["description"]
    assert entry["how_to_use"]
    assert entry["caution"]  # every indicator has one way to be misread


def test_the_catalogue_lookup_ignores_the_period():
    assert doc_for("ma_60") is INDICATOR_CATALOG["ma"]
    assert doc_for("rsi_21") is INDICATOR_CATALOG["rsi"]
    assert doc_for("plus_di_14") is INDICATOR_CATALOG["plus_di"]
    assert doc_for("something_else") is None


# --- 本轮新增的指标：BIAS / ROC / PSY / 量类 / 唐奇安 / SAR -------------------


def _constant_panel(values: list[list[float]], names: tuple[str, ...] = ("A.SH",)) -> PricePanel:
    """显式价格面板：不喂随机数，断言才不用和种子赌运气。"""
    import numpy as np
    import pandas as pd

    index = pd.bdate_range("2024-01-01", periods=len(values), name="date")
    close = pd.DataFrame(np.array(values, dtype=float), columns=names, index=index)
    return PricePanel(
        prices=close,
        extra={
            "open": close.copy(),
            "high": close * 1.01,
            "low": close * 0.99,
            "volume": pd.DataFrame(1000.0, index=index, columns=names),
            "amount": close * 1000.0 * 100,
        },
    )


def test_bias_is_zero_when_price_sits_on_its_average():
    from quant_research.indicators import bias

    # 30 天纹丝不动：MA 就是价格本身，乖离必须是 0
    frame = bias(_constant_panel([[100.0]] * 30).prices, 20)

    assert frame["A.SH"].iloc[-1] == pytest.approx(0.0)


def test_roc_compounds_not_adds():
    from quant_research.indicators import roc

    # 每日 +1% 复利 12 天 ≈ +12.68%，若有人误用加法会得 12.0
    values = [[100.0 * (1.01**i)] for i in range(15)]
    frame = roc(_constant_panel(values).prices, 12)

    assert frame["A.SH"].iloc[-1] == pytest.approx((1.01**12 - 1) * 100, rel=1e-9)


def test_psy_counts_days_not_magnitudes():
    """连涨 12 天 → 100；11 涨 1 跌 → 11/12。涨多少无关紧要。"""
    from quant_research.indicators import psy

    up = [[100.0 + i] for i in range(15)]
    assert psy(_constant_panel(up).prices, 12)["A.SH"].iloc[-1] == pytest.approx(100.0)

    mixed = [[100.0 + i * (1 if i != 7 else -1)] for i in range(15)]
    frame = psy(_constant_panel(mixed).prices, 12)
    # 第 8 天回撤破坏了一段连涨：窗口里恰有 1 天下跌
    assert 80 <= frame["A.SH"].iloc[-1] <= 92


def test_volume_ratio_is_one_for_flat_volume():
    from quant_research.indicators import volume_ratio

    panel = _constant_panel([[100.0]] * 10)
    frame = volume_ratio(panel.field("volume"), 5)

    assert frame["A.SH"].iloc[-1] == pytest.approx(1.0)


def test_mfi_saturates_when_typical_price_only_rises():
    from quant_research.indicators import mfi

    rising = [[100.0 + i] for i in range(20)]
    panel = _constant_panel(rising)
    frame = mfi(panel.field("high"), panel.field("low"), panel.prices, panel.field("volume"), 14)

    assert frame["A.SH"].iloc[-1] == pytest.approx(100.0)


def test_donchian_excludes_the_current_day():
    """突破判定的参照系必须不含当日：创出 101 的那天仍以 100 为上轨。"""
    from quant_research.indicators import donchian

    prices = [[100.0]] * 25 + [[101.0]]  # 最后一天创新高
    panel = _constant_panel(prices)
    channels = donchian(panel.field("high"), panel.field("low"), 20)

    # 高价字段是 close×1.01：前 20 日上轨 ≈ 100×1.01，当日 101×1.01 不进参照
    assert channels.upper["A.SH"].iloc[-1] == pytest.approx(101.0, rel=1e-9)


def test_sar_stays_below_price_in_an_uptrend_and_flips_on_a_break():
    from quant_research.indicators import sar

    up = [[100.0 + i * 0.5] for i in range(40)]
    panel = _constant_panel(up)
    frame = sar(panel.field("high"), panel.field("low"))

    series = frame["A.SH"].dropna()
    closes = panel.prices["A.SH"].reindex(series.index)
    assert (series < closes).all()  # 单边上行：SAR 始终压在价格下方
    assert series.is_monotonic_increasing  # 且逐日抬升——抛物线在追赶价格

    # 尾部崩塌 20%：SAR 必须翻到价格上方
    crash = up[:-5] + [[up[-1][0] * 0.8]] * 5
    frame = sar(_constant_panel(crash).field("high"), _constant_panel(crash).field("low"))
    last_sar, last_close = frame["A.SH"].iloc[-1], crash[-1][0]
    assert last_sar > last_close


def test_vr_mixes_up_and_down_volume():
    from quant_research.indicators import vr

    # 交替涨跌且成交量恒定：分子分母对半，VR ≈ 100（平盘量对半的约定在此不起作用）
    values = [[100.0 + (1.0 if i % 2 else -1.0)] for i in range(30)]
    panel = _constant_panel(values)
    frame = vr(panel.prices, panel.field("volume"), 26)

    assert frame["A.SH"].iloc[-1] == pytest.approx(100.0, rel=1e-9)


def test_compute_all_exposes_every_new_field():
    params = IndicatorParams()
    new_fields = {
        "bias_20",
        "roc_12",
        "psy_12",
        "volume_ratio_5",
        "vr_26",
        "mfi_14",
        "donchian_high_20",
        "donchian_low_20",
        "sar",
    }
    bundle = compute_all(_panel(days=80), params)

    assert new_fields <= set(bundle.frames.keys())
    assert all(doc_for(field) is not None for field in new_fields)
