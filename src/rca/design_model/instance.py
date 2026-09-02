"""
Module instance representation (hierarchy, Manual §6, §76).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .module import SourceLocation


class Instance(BaseModel):
    hierarchical_name: str
    local_name: str
    parent_module: str
    module_name: str           # referenced module definition
    parameter_overrides: dict[str, Any] = Field(default_factory=dict)
    port_connections: dict[str, str] = Field(default_factory=dict)  # formal -> actual
    is_blackbox: bool = False
    source_location: SourceLocation | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.hierarchical_name,
            "of": self.module_name,
            "ports": len(self.port_connections),
            "params": len(self.parameter_overrides),
            "blackbox": self.is_blackbox,
        }
