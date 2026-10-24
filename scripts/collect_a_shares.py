#!/usr/bin/env python
"""Standalone launcher for the daily A-share collector.

Works from a fresh checkout even when the package was not installed with ``-e``:

    python scripts/collect_a_shares.py --symbols 600519.SH --start 2024-01-01
    python scripts/collect_a_shares.py --all --start 2020-01-01      # slow
    python scripts/collect_a_shares.py --daemon --at 18:30           # keep running

Schedule it with cron instead if you prefer a short-lived process:

    30 18 * * 1-5  cd /path/to/quant-trading && .venv/bin/python scripts/collect_a_shares.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from quant_research.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
