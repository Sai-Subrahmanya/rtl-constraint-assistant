"""
Source manifest handling (Manual §10, §11).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config.model import ProjectConfig
from ..utils.logging import get_logger

log = get_logger("source")


def resolve_sources(cfg: ProjectConfig) -> list[Path]:
    """Resolve all source file paths to absolute Path objects, checking existence."""
    result: list[Path] = []
    if cfg.project_root is None:
        return result
    root = cfg.project_root
    for f in cfg.sources.files:
        p = Path(f)
        if not p.is_absolute():
            p = (root / p).resolve()
        if not p.is_file():
            log.warning("Source file not found: %s", p)
            continue
        result.append(p)
    return result


def resolve_include_dirs(cfg: ProjectConfig) -> list[Path]:
    result: list[Path] = []
    if cfg.project_root is None:
        return result
    root = cfg.project_root
    for d in cfg.sources.include_dirs:
        p = Path(d)
        if not p.is_absolute():
            p = (root / p).resolve()
        result.append(p)
    return result
