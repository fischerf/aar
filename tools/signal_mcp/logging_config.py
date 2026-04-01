"""
logging_config.py — Rich + stdlib logging setup for Signal MCP
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.logging import RichHandler

_console = Console(stderr=True)


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """
    Configure root logger with:
      • Rich formatted output to stderr
      • Optional plain-text rotation to a file
    Returns the 'signal_mcp' logger.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [
        RichHandler(
            console=_console,
            show_time=True,
            show_path=True,
            markup=True,
            rich_tracebacks=True,
            tracebacks_show_locals=True,
            level=numeric_level,
        )
    ]

    if log_file:
        file_path = Path(log_file).expanduser()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(file_path, encoding="utf-8")
        fh.setLevel(numeric_level)
        fh.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        handlers.append(fh)

    logging.basicConfig(
        level=numeric_level,
        handlers=handlers,
        force=True,
    )

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "asyncio", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger("signal_mcp")
    logger.setLevel(numeric_level)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the signal_mcp namespace."""
    return logging.getLogger(f"signal_mcp.{name}")
