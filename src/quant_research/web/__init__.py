"""Web workbench: a FastAPI shell over the research library.

The UI computes nothing of its own — see :mod:`quant_research.web.api`.
Optional dependency: ``pip install -e '.[web]'``.
"""

from __future__ import annotations

from .app import create_app, main

__all__ = ["create_app", "main"]
