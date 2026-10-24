"""FastAPI application factory and the ``quant-ui`` entry point.

Bound to ``127.0.0.1`` by default: this tool exposes local market data and the
ability to trigger collection, so it is a single-user, on-host service by design.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from quant_research.logging_setup import configure_logging, get_logger

from .api import router

logger = get_logger("web.app")

STATIC_DIR = Path(__file__).parent / "static"
APP_MODULE = "quant_research.web.app:create_app"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Quant Research Workbench",
        description=(
            "Read-mostly shell over the quant_research library. Every number is "
            "computed by the library, never by the UI."
        ),
        version="0.1.0",
    )
    app.include_router(router)
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:  # pragma: no cover - only when the package is shipped without assets
        logger.warning("static directory missing: %s", STATIC_DIR)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant-ui", description="Start the local web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default localhost)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="reload on code changes")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    configure_logging()
    import uvicorn

    logger.info("starting workbench on http://%s:%d", args.host, args.port)
    uvicorn.run(
        APP_MODULE,
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


__all__ = ["create_app", "main"]
