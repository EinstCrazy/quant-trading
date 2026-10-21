"""Generate the synthetic sample dataset shipped with the repository.

The output is *fake* market data: it exists so that the README example, the test
suite and the backtest engine can all run offline. It carries no information
about any real security and must never be used to draw research conclusions.

Usage::

    python scripts/generate_sample_data.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SEED = 20240101
N_DAYS = 504
START = "2022-01-03"
SYMBOLS = {
    "ALPHA": {"drift": 0.0004, "vol": 0.012, "start": 100.0},
    "BETA": {"drift": 0.0002, "vol": 0.018, "start": 50.0},
    "GAMMA": {"drift": -0.0001, "vol": 0.025, "start": 25.0},
}
OUTPUT = Path(__file__).resolve().parent.parent / "data" / "sample" / "sample_prices.csv"


def build_frame() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    dates = pd.bdate_range(START, periods=N_DAYS, name="date")

    frames = []
    for symbol, params in SYMBOLS.items():
        shocks = rng.normal(params["drift"], params["vol"], N_DAYS)
        close = params["start"] * np.cumprod(1 + shocks)
        prev_close = np.concatenate([[params["start"]], close[:-1]])
        gap = rng.normal(0.0, params["vol"] / 3, N_DAYS)
        open_ = prev_close * (1 + gap)
        spread = np.abs(rng.normal(0.0, params["vol"] / 2, N_DAYS)) + 0.001
        high = np.maximum(open_, close) * (1 + spread)
        low = np.minimum(open_, close) * (1 - spread)
        volume = rng.integers(100_000, 1_000_000, N_DAYS)

        frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "symbol": symbol,
                    "open": np.round(open_, 4),
                    "high": np.round(high, 4),
                    "low": np.round(low, 4),
                    "close": np.round(close, 4),
                    "volume": volume,
                }
            )
        )

    return pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"])


def main() -> None:
    frame = build_frame()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(OUTPUT, index=False, float_format="%.4f")
    print(f"wrote {len(frame)} rows to {OUTPUT}")


if __name__ == "__main__":
    main()
