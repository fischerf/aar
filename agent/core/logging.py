"""Unified logging setup for all aar entry points."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def configure_logging(
    level: str = "WARNING",
    log_file: Path | None = None,
) -> None:
    """Configure the Python logging hierarchy for aar.

    - Always adds a stderr ``StreamHandler`` (12-factor / container-friendly).
    - Optionally adds a ``FileHandler`` when *log_file* is provided.
    - Silences noisy HTTP internals above DEBUG.
    - Idempotent — safe to call more than once (won't duplicate the stderr handler).
    """
    numeric = getattr(logging, level.upper(), logging.WARNING)

    root = logging.getLogger()
    root.setLevel(numeric)

    fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")

    # stderr handler — add only once
    has_stderr = any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stderr for h in root.handlers
    )
    if not has_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # file handler — opt-in, append mode, with timestamps
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)

    # silence noisy HTTP internals, or un-silence them at DEBUG
    for name in ("httpx", "httpcore", "asyncio"):
        if numeric > logging.DEBUG:
            logging.getLogger(name).setLevel(logging.WARNING)
        else:
            logging.getLogger(name).setLevel(logging.NOTSET)
