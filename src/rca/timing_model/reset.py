"""
Reset detection and representation (Manual §14).

Reset candidates are classified by structural evidence, not by name:

* ASYNCHRONOUS reset — signal appears on the sensitivity list (edge
  control) AND has a reset branch in the procedural body (e.g.
  ``if (!rst_n) q <= '0;``).
* SYNCHRONOUS reset — signal controls a reset-value assignment inside
  an edge-triggered process but does NOT appear on the sensitivity list
  (so the reset action takes effect only on the next active clock edge).
* UNKNOWN — signal is used to condition a constant assignment but we
  cannot confirm its semantics (e.g. gated without a constant value,
  complex predicate, etc.).

Active-high vs active-low is determined from the polarity of the
predicate at the reset branch (``if (rst)`` → active_high,
``if (!rst_n)`` → active_low) combined with the sensitivity edge.
Naming (``rst_n``) is recorded as weak evidence only.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ..design_model import SourceLocation
from ..utils.enums import ClockEdge, ResetPolarity, ResetType


class ResetEvidenceKind(str, Enum):
    EDGE_SENSITIVE = "edge_sensitive"      # on posedge/negedge in sensitivity
    RESET_BRANCH = "reset_branch"          # if (rst) q <= CONSTANT detected
    SYNC_CONTROL = "sync_control"          # controls constant-load but NOT on sensitivity
    REGISTER_ASSIGN = "register_assign"    # fans out to registers
    NAMING_HINT = "naming_hint"            # name looks reset-ish (weak)
    USER_DECLARED = "user_declared"


class ResetEvidence(BaseModel):
    kind: ResetEvidenceKind
    detail: str
    source: str | None = None


class Reset(BaseModel):
    id: str
    name: str
    source_object: str                    # hierarchical signal/port
    reset_type: ResetType = ResetType.UNKNOWN
    edge: ClockEdge | None = None        # edge on sensitivity for async resets
    polarity: ResetPolarity = ResetPolarity.UNKNOWN
    registers_driven: list[str] = Field(default_factory=list)
    processes: list[str] = Field(default_factory=list)
    domain_id: str | None = None
    associated_clock: str | None = None
    source_of_value: str = "INFERENCE"   # INFERENCE | USER | EXISTING_SDC
    confidence: str = "UNKNOWN"
    evidence: list[ResetEvidence] = Field(default_factory=list)
    is_top_level_port: bool = False
    source_location: SourceLocation | None = None
    notes: list[str] = Field(default_factory=list)

    def add_evidence(self, kind: ResetEvidenceKind, detail: str,
                     source: str | None = None) -> None:
        for e in self.evidence:
            if e.kind == kind and e.detail == detail:
                return
        self.evidence.append(ResetEvidence(kind=kind, detail=detail, source=source))
        self._recompute_confidence()

    def _recompute_confidence(self) -> None:
        if self.source_of_value == "USER":
            self.confidence = "HIGH"
            return
        if not self.evidence:
            self.confidence = "UNKNOWN"
            return
        kinds = {e.kind for e in self.evidence}
        # Async reset: edge-sensitive AND reset-branch → HIGH.
        if (ResetEvidenceKind.EDGE_SENSITIVE in kinds
                and ResetEvidenceKind.RESET_BRANCH in kinds):
            self.confidence = "HIGH"
        elif ResetEvidenceKind.SYNC_CONTROL in kinds:
            self.confidence = "MEDIUM"
        elif ResetEvidenceKind.EDGE_SENSITIVE in kinds:
            self.confidence = "MEDIUM"
        elif kinds == {ResetEvidenceKind.NAMING_HINT}:
            self.confidence = "LOW"
        else:
            self.confidence = "LOW"

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.reset_type.value,
            "polarity": self.polarity.value,
            "edge": self.edge.value if self.edge else None,
            "driven_registers": len(self.registers_driven),
            "associated_clock": self.associated_clock,
            "confidence": self.confidence,
            "evidence_kinds": sorted({e.kind.value for e in self.evidence}),
            "notes": self.notes,
        }
