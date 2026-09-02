"""
Process (always/initial/final block) representation.

Used heavily by clock/reset discovery (Manual §13, §14).  As of the
connectivity-aware refactor the process also records:

* ``assigned_signals`` — full list of LHS targets (hierarchical names).
* ``read_signals`` — every signal read anywhere in the body (control +
  data), hierarchical names, de-duplicated and preserving order.
* ``clock_signals`` / ``reset_signals`` — control signals identified from
  the sensitivity list (posedge/negedge), separated from data.
* ``control_signals`` — condition/select/if/while dependencies identified
  as control flow (distinct from data).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..utils.enums import ClockEdge
from .module import SourceLocation


class SensitivityItem(BaseModel):
    signal: str
    edge: ClockEdge | None = None  # posedge/negedge if explicit


class Process(BaseModel):
    id: str                                  # unique id e.g. "top.counter.proc1"
    parent_module: str
    kind: str                                # always_ff/always_comb/always_latch/always/initial/final
    sensitivity: list[SensitivityItem] = Field(default_factory=list)

    # Populated by the parser:
    assigned_signals: list[str] = Field(default_factory=list)
    read_signals: list[str] = Field(default_factory=list)
    control_signals: list[str] = Field(default_factory=list)  # if/select/condition inputs

    # Discovered control signals (separated from data):
    clock_signals: list[str] = Field(default_factory=list)
    reset_signals: list[str] = Field(default_factory=list)

    has_reset_branch: bool = False
    has_enable_branch: bool = False
    inferred_clock: str | None = None
    inferred_reset: str | None = None
    inferred_reset_edge: ClockEdge | None = None
    source_location: SourceLocation | None = None

    def is_edge_triggered(self) -> bool:
        return any(s.edge is not None for s in self.sensitivity)

    def clock_candidates(self) -> list[str]:
        """Signals mentioned with posedge/negedge — strong clock evidence."""
        return [s.signal for s in self.sensitivity if s.edge is not None]

    def data_read_signals(self) -> list[str]:
        """Signals read as *data* (read_signals minus clock/reset/control)."""
        ctl = set(self.clock_signals) | set(self.reset_signals) | set(self.control_signals)
        return [s for s in self.read_signals if s not in ctl]

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "clock": self.inferred_clock,
            "reset": self.inferred_reset,
            "assigned": self.assigned_signals,
            "reads": self.read_signals,
        }
