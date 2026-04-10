"""Tests for agent.core.logging — unified logging setup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from agent.core.logging import configure_logging

_NOISY_LIBS = ("httpx", "httpcore", "asyncio")


@pytest.fixture(autouse=True)
def _clean_root_logger():
    """Snapshot root logger handlers before each test and restore after."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_lib_levels = {name: logging.getLogger(name).level for name in _NOISY_LIBS}
    yield
    root.handlers = original_handlers
    root.level = original_level
    for name, lvl in original_lib_levels.items():
        logging.getLogger(name).setLevel(lvl)


class TestConfigureLogging:
    def test_adds_stderr_handler(self):
        configure_logging("INFO")
        root = logging.getLogger()
        stderr_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        ]
        assert len(stderr_handlers) >= 1
        assert root.level == logging.INFO

    def test_file_handler(self, tmp_path: Path):
        log_file = tmp_path / "sub" / "aar.log"
        configure_logging("DEBUG", log_file=log_file)
        root = logging.getLogger()

        file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) >= 1
        assert log_file.parent.exists()

        # Write a message and verify it lands in the file
        test_logger = logging.getLogger("test.file_handler")
        test_logger.info("hello from test")
        for h in file_handlers:
            h.flush()
        assert "hello from test" in log_file.read_text(encoding="utf-8")

    def test_idempotent(self):
        configure_logging("WARNING")
        root = logging.getLogger()
        count_before = sum(
            1
            for h in root.handlers
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        )

        configure_logging("WARNING")
        count_after = sum(
            1
            for h in root.handlers
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        )
        assert count_after == count_before

    def test_silences_noisy_libs(self):
        configure_logging("WARNING")
        for name in ("httpx", "httpcore", "asyncio"):
            assert logging.getLogger(name).level == logging.WARNING

    def test_noisy_libs_not_silenced_at_debug(self):
        configure_logging("DEBUG")
        # At DEBUG, we should NOT force these to WARNING
        # (they keep whatever level they had, which is NOTSET/0 by default)
        for name in ("httpx", "httpcore", "asyncio"):
            assert logging.getLogger(name).level != logging.WARNING
