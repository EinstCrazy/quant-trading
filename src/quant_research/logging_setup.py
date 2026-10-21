"""Central logging configuration for the research package.

Modules should obtain a logger through :func:`get_logger` instead of using
``print``. Log records describe *process* (which files were read, how many rows
were dropped, whether costs were clipped); research conclusions are returned as
values and written to ``reports/`` instead of being logged.
"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "quant_research"
_CONFIGURED = False


def configure_logging(level: int = logging.INFO, stream=None) -> None:
    """Attach a stream handler to the package logger.

    Calling this more than once is safe; subsequent calls only adjust the level.
    """
    global _CONFIGURED

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)

    if not _CONFIGURED:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger nested under the package logger."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(f"{_LOGGER_NAME}.{name}") if name else logging.getLogger(_LOGGER_NAME)


__all__ = ["configure_logging", "get_logger"]
