"""
Module representation (Manual §7.2).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SourceLocation(BaseModel):
    """A source-code reference."""
    file: str
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:
        s = self.file
        if self.line is not None:
            s += f":{self.line}"
            if self.column is not None:
                s += f":{self.column}"
        return s


class Module(BaseModel):
    """Normalised representation of a Verilog/SystemVerilog module."""
    name: str
    source_files: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    localparams: dict[str, Any] = Field(default_factory=dict)
    port_names: list[str] = Field(default_factory=list)
    # references populated when the design is built
    signals: set[str] = Field(default_factory=set)
    instance_names: list[str] = Field(default_factory=list)
    process_ids: list[str] = Field(default_factory=list)
    continuous_assignments: int = 0
    source_locations: list[SourceLocation] = Field(default_factory=list)
    is_top: bool = False

    # --- Back-references populated by Design ---
    # ports: dict[str, Port] handled at design level
    # instances: dict[str, Instance]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ports": len(self.port_names),
            "signals": len(self.signals),
            "instances": len(self.instance_names),
            "processes": len(self.process_ids),
            "assigns": self.continuous_assignments,
            "is_top": self.is_top,
        }
