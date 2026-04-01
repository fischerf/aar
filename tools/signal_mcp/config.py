"""
config.py — Configuration management for Signal MCP Server
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

CONFIG_PATH = Path(os.getenv("SIGNAL_MCP_CONFIG", "~/.signal-mcp/config.json")).expanduser()


class SignalConfig(BaseModel):
    """Validated configuration model."""

    # Signal REST API
    api_base_url: str = Field(
        default_factory=lambda: os.getenv("SIGNAL_API_URL", "http://localhost:8080"),
        description="Base URL of the signal-cli REST API",
    )
    api_timeout: float = Field(
        default=30.0,
        description="Default HTTP request timeout in seconds",
    )
    receive_timeout: float = Field(
        default=10.0,
        description="Timeout for polling new messages in seconds",
    )

    # Account
    account_number: Optional[str] = Field(
        default_factory=lambda: os.getenv("SIGNAL_PHONE_NUMBER"),
        description="Registered Signal phone number (E.164 format, e.g. +15551234567)",
    )

    # Logging
    log_level: str = Field(
        default_factory=lambda: os.getenv("SIGNAL_LOG_LEVEL", "INFO"),
        description="Logging verbosity: DEBUG | INFO | WARNING | ERROR",
    )
    log_file: Optional[str] = Field(
        default_factory=lambda: os.getenv("SIGNAL_LOG_FILE"),
        description="Optional file path for persistent log output",
    )

    # Polling
    poll_interval: float = Field(
        default=5.0,
        description="Seconds between passive receive polls",
    )

    class Config:
        extra = "allow"


def load_config() -> SignalConfig:
    """Load config from file, falling back to env / defaults."""
    data: dict = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return SignalConfig(**data)


def save_config(cfg: SignalConfig) -> None:
    """Persist config to disk."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2))
