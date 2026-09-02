"""
Clock model (Manual §7.5, §13, §19, §20, §21).

Each clock is backed by explicit **evidence** records. Evidence is
classified into discrete categories with a well-defined strength so
that name-based heuristics can never be mistaken for structural proof:

* ``edge_sensitive``  — the signal appears on a ``posedge``/``negedge``
  in a sequential (always_ff/always) sensitivity list. This is strong
  evidence.
* ``drives_register`` — a register's D->Q transition is clocked by this
  signal (``Register.clock_signal``). Strong evidence.
* ``sequential_process`` — a sequential process lists this signal as a
  clock. Strong evidence.
* ``top_level_port`` — the signal is a top-level port; supports but
  does not prove a clock.
* ``user_declared``   — explicit user clock constraint (highest).
* ``generated_clock`` — derived from another clock via a divider/mux/gate.
* ``naming_hint``     — weak: name matches ``clk*``/``*_clk`` pattern.
  NEVER promotes confidence to HIGH on its own.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..design_model import SourceLocation
from ..utils.enums import ClockEdge


class ClockEvidenceKind(str, Enum):
    EDGE_SENSITIVE = "edge_sensitive"
    DRIVES_REGISTER = "drives_register"
    SEQUENTIAL_PROCESS = "sequential_process"
    TOP_LEVEL_PORT = "top_level_port"
    USER_DECLARED = "user_declared"
    GENERATED_CLOCK = "generated_clock"
    GATED_CLOCK = "gated_clock"
    CLOCK_MUX = "clock_mux"
    NAMING_HINT = "naming_hint"


# Evidence-strength ranking (higher = stronger).  Used to compute
# overall clock confidence deterministically from collected evidence.
_EVIDENCE_STRENGTH: dict[ClockEvidenceKind, int] = {
    ClockEvidenceKind.USER_DECLARED:       4,
    ClockEvidenceKind.DRIVES_REGISTER:     3,
    ClockEvidenceKind.EDGE_SENSITIVE:      3,
    ClockEvidenceKind.SEQUENTIAL_PROCESS:  3,
    ClockEvidenceKind.GENERATED_CLOCK:     2,
    ClockEvidenceKind.GATED_CLOCK:         2,
    ClockEvidenceKind.CLOCK_MUX:           2,
    ClockEvidenceKind.TOP_LEVEL_PORT:      1,
    ClockEvidenceKind.NAMING_HINT:         0,
}


class ClockEvidence(BaseModel):
    """A single piece of evidence supporting a clock candidate."""
    kind: ClockEvidenceKind
    detail: str
    source: str | None = None   # optional path to source location/proc id


class Clock(BaseModel):
    id: str
    name: str
    source_object: str                              # port or pin
    source_port_or_pin: str = ""
    edge: ClockEdge = ClockEdge.POSEDGE
    period_seconds: float | None = None
    waveform: list[float] | None = None             # [rise, fall] in seconds
    parent_clock: str | None = None                 # for generated clocks
    generation_expression: str | None = None
    divide_by: int | None = None
    multiply_by: int | None = None
    is_generated: bool = False
    is_gated: bool = False
    is_mux: bool = False
    mux_select_signal: str | None = None
    mux_sources: list[str] = Field(default_factory=list)
    gate_enable_signal: str | None = None
    domain_id: str | None = None
    mode: str | None = None
    uncertainty_seconds: float | None = None
    latency_seconds: float | None = None
    source_of_value: str = "INFERENCE"               # INFERENCE|USER|EXISTING_SDC
    confidence: str = "UNKNOWN"
    status: str = "PROPOSED"                         # PROPOSED|CONFIRMED|FIXED|...
    registers_driven: list[str] = Field(default_factory=list)
    processes: list[str] = Field(default_factory=list)
    evidence: list[ClockEvidence] = Field(default_factory=list)
    is_top_level_port: bool = False
    source_location: SourceLocation | None = None
    # Free-form additional notes (ambiguities, warnings).
    notes: list[str] = Field(default_factory=list)

    # --- helpers ---

    def period_ns(self) -> float | None:
        if self.period_seconds is None:
            return None
        return self.period_seconds * 1e9

    def frequency_mhz(self) -> float | None:
        if self.period_seconds is None:
            return None
        return 1e-6 / self.period_seconds

    def is_primary(self) -> bool:
        return self.parent_clock is None and not self.is_generated

    def _recompute_confidence(self) -> None:
        """Deterministically set ``confidence`` from current evidence."""
        if self.source_of_value == "USER":
            self.confidence = "HIGH"
            return
        if not self.evidence:
            self.confidence = "UNKNOWN"
            return
        best = max(_EVIDENCE_STRENGTH.get(e.kind, 0) for e in self.evidence)
        has_naming_only = all(e.kind == ClockEvidenceKind.NAMING_HINT
                              for e in self.evidence)
        if has_naming_only:
            self.confidence = "LOW"
        elif best >= 3:
            self.confidence = "HIGH"
        elif best >= 2:
            self.confidence = "MEDIUM"
        elif best >= 1:
            self.confidence = "LOW"
        else:
            self.confidence = "UNKNOWN"

    def add_evidence(self, kind: ClockEvidenceKind, detail: str,
                     source: str | None = None) -> None:
        """Add an evidence record and refresh confidence."""
        # De-duplicate on (kind, detail).
        for e in self.evidence:
            if e.kind == kind and e.detail == detail:
                return
        self.evidence.append(ClockEvidence(kind=kind, detail=detail, source=source))
        self._recompute_confidence()

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source_object,
            "edge": self.edge.value,
            "period_ns": self.period_ns(),
            "frequency_mhz": self.frequency_mhz(),
            "is_generated": self.is_generated,
            "is_gated": self.is_gated,
            "is_mux": self.is_mux,
            "parent_clock": self.parent_clock,
            "driven_registers": len(self.registers_driven),
            "processes": len(self.processes),
            "confidence": self.confidence,
            "status": self.status,
            "source_of_value": self.source_of_value,
            "evidence_kinds": sorted({e.kind.value for e in self.evidence}),
            "notes": self.notes,
        }
