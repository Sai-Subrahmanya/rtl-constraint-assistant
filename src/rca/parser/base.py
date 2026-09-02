"""
Abstract parser adapter interface (Manual §5.2, §51).

All RTL frontends must implement this interface so the rest of RCA
does not depend on any one parser's API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..design_model import Design
from .diagnostics import DiagnosticCollector


class ParserAdapter(ABC):
    """Abstract base for RTL parser adapters."""

    name: str = "base"

    def __init__(self) -> None:
        self.diagnostics = DiagnosticCollector()

    @abstractmethod
    def parse(
        self,
        files: list[str | Path],
        include_dirs: list[str | Path] | None = None,
        defines: dict[str, str] | list[str] | None = None,
        top: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Design:
        """Parse & elaborate the given source files into a Design model.

        Parameters
        ----------
        files : list of paths to Verilog/SystemVerilog sources
        include_dirs : include search paths
        defines : preprocessor defines (dict or list of "NAME=VAL" / "NAME")
        top : optional top module override
        parameters : top-level parameter overrides
        """

    def parse_files(
        self,
        files: list[str | Path],
        include_dirs: list[str] | None = None,
        defines: list[str] | None = None,
        top: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Design:
        """Convenience wrapper accepting defines as a list (YAML style)."""
        defines_dict: dict[str, str] = {}
        if defines:
            for d in defines:
                if "=" in d:
                    k, v = d.split("=", 1)
                    defines_dict[k.strip()] = v.strip()
                else:
                    defines_dict[d.strip()] = ""
        return self.parse(
            files=files,
            include_dirs=include_dirs or [],
            defines=defines_dict,
            top=top,
            parameters=parameters or {},
        )
