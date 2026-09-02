"""
Port representation (Manual §7.3).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..utils.enums import PortDirection
from .module import SourceLocation


class Port(BaseModel):
    hierarchical_name: str
    local_name: str
    direction: PortDirection
    width: int = 1
    width_spec: str | None = None  # e.g. "[7:0]"
    signed: bool = False
    datatype: str = "logic"
    net_kind: str = "wire"
    parent_module: str
    source_location: SourceLocation | None = None
    connected_clock_candidates: list[str] = Field(default_factory=list)
    timing_role_candidates: list[str] = Field(default_factory=list)

    @property
    def is_clock_like(self) -> bool:
        return "clock" in self.timing_role_candidates or bool(self.connected_clock_candidates)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.hierarchical_name,
            "direction": self.direction.value,
            "width": self.width,
            "clock_candidate": self.is_clock_like,
        }
