"""
Structured logging configuration for RCA (Manual §68).

Supports DEBUG/INFO/WARNING/ERROR/CRITICAL levels. Console output uses
``rich`` for readability; machine-readable JSON logging is available
for optimization runs and batch mode.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

# Shared console for Rich output
console = Console(stderr=False)
err_console = Console(stderr=True)

_CONFIGURED = False


def configure_logging(level: str = "INFO", json_log_path: str | Path | None = None) -> None:
    """Set up RCA logging. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger("rca").setLevel(level.upper())
        return

    root = logging.getLogger("rca")
    root.setLevel(level.upper())
    root.propagate = False

    # Console via Rich
    rich_handler = RichHandler(
        console=err_console,
        show_time=False,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
    )
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(rich_handler)

    if json_log_path:
        jp = Path(json_log_path)
        jp.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(jp, mode="w", encoding="utf-8")
        fh.setFormatter(_JsonFormatter())
        root.addHandler(fh)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under ``rca.<name>``."""
    return logging.getLogger(f"rca.{name}")


class _JsonFormatter(logging.Formatter):
    """Emits one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
