"""
Inference rule registry and result model (Manual §119, §120, §121).

Architectural boundary:

* **Rules** inspect evidence from Design + TimingGraph and produce an
  :class:`InferenceResult`. They may propose constraints, flag
  ambiguities, log warnings, attach evidence, and record missing
  information. They MUST NOT mutate the ConstraintSet or the
  AssumptionLedger directly.
* **InferenceEngine** executes rules, validates results, materializes
  proposals into the UCM, attaches provenance, merges duplicates, and
  reports missing information. No inference decision is hidden inside
  materialization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..provenance import Evidence
from ..utils.enums import (
    Confidence,
    InferenceResultStatus,
    RequirementLevel,
)


@dataclass
class MissingInformation:
    """Structured record of something RCA needs but cannot determine
    from available evidence."""

    id: str
    category: str
    object: str
    severity: str = "WARNING"
    requirement_level: RequirementLevel = RequirementLevel.REQUIRED
    message: str = ""
    rationale: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    suggested_inputs: list[dict[str, Any]] = field(default_factory=list)
    blocking: bool = True
    rule_id: str | None = None
    possible_values: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "object": self.object,
            "severity": self.severity,
            "requirement_level": self.requirement_level.value,
            "message": self.message,
            "rationale": self.rationale,
            "evidence": list(self.evidence),
            "suggested_inputs": list(self.suggested_inputs),
            "blocking": self.blocking,
            "rule_id": self.rule_id,
            "possible_values": list(self.possible_values),
        }


@dataclass
class ProposedConstraint:
    """A constraint proposed by a rule. Carries everything needed to
    build a UCM Constraint later; the engine maps these to concrete
    types (CREATE_CLOCK, SET_INPUT_DELAY, ...) and attaches
    rule-originated provenance."""

    kind: str                              # e.g. "create_clock", "set_input_delay", ...
    object: str                            # name of the design object targeted
    clock: str | None = None
    period_seconds: float | None = None
    delay_seconds: float | None = None
    values: dict[str, Any] = field(default_factory=dict)
    confidence: Confidence = Confidence.MEDIUM
    status: str = "PROPOSED"               # "FIXED" | "CONFIRMED" | "PROPOSED" | ...
    source_kind: str = "INFERENCE"
    evidence: list[Evidence] = field(default_factory=list)
    rationale: str = ""
    path_selector: dict[str, Any] | None = None
    target_objects: list[str] = field(default_factory=list)
    source_objects: list[str] = field(default_factory=list)
    scenario_ids: list[str] = field(default_factory=list)
    assumption_ids: list[str] = field(default_factory=list)
    merge_key: tuple | None = None         # if set, duplicates merged by this key

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "object": self.object,
            "clock": self.clock,
            "period_seconds": self.period_seconds,
            "delay_seconds": self.delay_seconds,
            "values": dict(self.values),
            "confidence": self.confidence.value if isinstance(self.confidence, Confidence) else self.confidence,
            "status": self.status,
            "source_kind": self.source_kind,
            "rationale": self.rationale,
        }


@dataclass
class InferenceResult:
    """What a rule produces.

    The rule only reports what it found; the engine turns these into
    UCM constraints (and may combine evidence across rules).
    """

    rule_id: str
    rule_name: str
    result_status: InferenceResultStatus = InferenceResultStatus.NO_FINDING
    confidence: Confidence = Confidence.UNKNOWN
    proposed_constraints: list[ProposedConstraint] = field(default_factory=list)
    assumptions_added: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    missing_information: list[MissingInformation] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    # ---- convenience mutators ----

    def propose(self, pc: ProposedConstraint) -> None:
        self.proposed_constraints.append(pc)
        # If we produced any proposals, status is at least PROPOSED
        # unless a stricter status was already set.
        if self.result_status == InferenceResultStatus.NO_FINDING:
            self.result_status = InferenceResultStatus.PROPOSED

    def add_warning(self, msg: str, **kw: Any) -> None:
        self.warnings.append({"message": msg, **kw})

    def add_ambiguity(self, msg: str, **kw: Any) -> None:
        self.ambiguities.append({"message": msg, **kw})
        if self.result_status in (InferenceResultStatus.NO_FINDING, InferenceResultStatus.PROPOSED):
            self.result_status = InferenceResultStatus.REQUIRES_CONFIRMATION

    def add_evidence(self, ev: Evidence) -> None:
        self.evidence.append(ev)

    def add_missing(self, mi: MissingInformation) -> None:
        self.missing_information.append(mi)
        if mi.blocking and self.result_status in (InferenceResultStatus.NO_FINDING, InferenceResultStatus.PROPOSED):
            self.result_status = InferenceResultStatus.BLOCKED

    def add_conflict(self, msg: str, **kw: Any) -> None:
        self.conflicts.append({"message": msg, **kw})

    # ---- backward-compat accessors for older code/test expectations ----

    @property
    def constraints_added(self) -> list[dict[str, Any]]:
        return [pc.to_dict() for pc in self.proposed_constraints]

    def add_constraint(self, desc: str, **kw: Any) -> None:
        """Deprecated shim: adapts old add_constraint(desc, **kw) calls."""
        pc = ProposedConstraint(
            kind=kw.pop("kind", kw.pop("type", "unknown")),
            object=kw.pop("object", kw.pop("clock", kw.pop("port", kw.get("name", "")))),
            clock=kw.pop("clock", None),
            period_seconds=kw.pop("period", kw.pop("period_seconds", None)),
            delay_seconds=kw.pop("delay", kw.pop("delay_seconds", None)),
            values={k: v for k, v in kw.items() if k not in {"confidence", "status", "source_kind"}},
            confidence=Confidence(kw["confidence"].upper()) if isinstance(kw.get("confidence"), str) else (kw.get("confidence") or Confidence.MEDIUM),
            status=kw.get("status", "PROPOSED"),
            source_kind=kw.get("source", kw.get("source_kind", "INFERENCE")),
            rationale=desc,
        )
        self.propose(pc)


@dataclass
class Rule:
    id: str
    name: str
    applies: Callable[..., bool]
    infer: Callable[..., InferenceResult]
    confidence: str = "MEDIUM"
    description: str = ""
    required_inputs: list[str] = field(default_factory=list)
