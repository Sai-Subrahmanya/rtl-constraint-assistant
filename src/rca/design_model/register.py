"""
Register / sequential element representation (Manual §7.4, §15).

The connectivity-aware model tracks both the structural D-input
dependency (``data_sources``: every signal that appears in the data
expression) and the downstream fanout of the Q output (``q_consumers``:
signals/registers/ports that structurally depend on Q).  ``data_source``
is retained for backward compatibility as the *preferred single source*
when the assignment is a plain ``q <= x;``, otherwise ``None``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..utils.enums import ClockEdge, ResetPolarity, ResetType
from .module import SourceLocation


class Register(BaseModel):
    hierarchical_name: str
    local_name: str
    parent_module: str
    width: int = 1

    clock_signal: str | None = None
    clock_edge: ClockEdge = ClockEdge.POSEDGE
    reset_signal: str | None = None
    reset_type: ResetType = ResetType.UNKNOWN
    reset_edge: ClockEdge | None = None
    reset_polarity: ResetPolarity = ResetPolarity.UNKNOWN
    enable_signal: str | None = None

    # Structural D-input cone: every signal that feeds the non-reset data
    # assignment (populated by the parser).  These are signals whose
    # values contribute to the *value* loaded into the register on a
    # clock edge (the true D-cone), including operands of arithmetic,
    # bitwise, logical, and comparison operators appearing on the RHS,
    # and the value-branches of ternaries.
    data_sources: list[str] = Field(default_factory=list)

    # Control/predicate inputs: signals that determine *whether* a
    # particular assignment fires (if/case predicates) or *which* value
    # is selected (ternary selectors).  These are NOT part of the D
    # data cone but are tracked separately so enables, mux selects,
    # and clock-gating candidates can be identified.
    control_sources: list[str] = Field(default_factory=list)

    # Structural Q-output fanout (populated by Design.build_connectivity).
    q_consumers: list[str] = Field(default_factory=list)

    # Convenience: a single preferred source (set when the assignment RHS
    # is a single named signal, otherwise None).  Back-compat field.
    data_source: str | None = None

    inferred_type: str = "dff"  # dff | dffe | latch | other
    source_location: SourceLocation | None = None

    # Owning process id (for provenance).
    process_id: str | None = None

    @property
    def is_asynchronous_reset(self) -> bool:
        return self.reset_type == ResetType.ASYNCHRONOUS

    def q_name(self) -> str:
        """Hierarchical name of this register's Q output (identical to the
        net that holds the register state)."""
        return self.hierarchical_name

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.hierarchical_name,
            "clock": self.clock_signal,
            "edge": self.clock_edge.value,
            "reset": self.reset_signal,
            "reset_type": self.reset_type.value,
            "enable": self.enable_signal,
            "data_sources": self.data_sources,
        }
