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

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..provenance import Evidence, ProvenanceRecord
from ..utils.enums import (
    Confidence,
    InferenceResultStatus,
    RequirementLevel,
)


class InferenceStatus(str, Enum):
    """Status of one advisory inference candidate.

    This is deliberately separate from UCM lifecycle status, UCM confidence,
    knowledge trust, and validation status. A candidate remains advisory until
    an explicit caller accepts it into a ``ConstraintSet``.
    """

    INFERRED = "INFERRED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING = "CONFLICTING"
    REJECTED = "REJECTED"


class InferenceDecision(str, Enum):
    """Whether a caller may explicitly attempt UCM acceptance."""

    ACCEPTABLE = "ACCEPTABLE"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    REJECTED = "REJECTED"


# Candidate evidence intentionally reuses the canonical RCA Evidence model;
# this alias documents the inference-facing API without a second provenance
# representation.
InferenceEvidence = Evidence


@dataclass(frozen=True)
class InferenceCandidate:
    """A deterministic, non-UCM inference recommendation.

    ``constraint_template`` is an optional copied canonical UCM constraint. It
    is evidence for a *possible* later explicit action, not an accepted or
    emittable constraint.
    """

    id: str
    kind: str
    constraint_type: str | None
    status: InferenceStatus
    decision: InferenceDecision
    rule_ids: tuple[str, ...]
    analysis: str
    source_objects: tuple[str, ...]
    clock_refs: tuple[str, ...]
    port_refs: tuple[str, ...]
    register_refs: tuple[str, ...]
    evidence: tuple[InferenceEvidence, ...]
    provenance: ProvenanceRecord
    rationale: str
    source_snapshot_identity: dict[str, str]
    # Digest of the canonical proposal(s), distinct from candidate ID and all
    # lifecycle/validation/trust labels. Step 28 combines semantic normalization
    # and canonical-template content in this digest before UCM mutation, so a
    # caller cannot strip reviewed links/provenance from serialized templates.
    candidate_semantic_identity: str | None = None
    constraint_template: dict[str, Any] | None = None
    # A backward-compatible extension for an advisory finding that needs a
    # coordinated set of UCM constraints. An empty tuple means use the legacy
    # single ``constraint_template`` field. It is still advisory-only.
    constraint_templates: tuple[dict[str, Any], ...] = ()
    knowledge_references: tuple[dict[str, Any], ...] = ()
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    missing_information: tuple[dict[str, Any], ...] = ()
    existing_constraint_ids: tuple[str, ...] = ()
    # Mutually possible structural interpretations are retained as hypotheses,
    # never collapsed into a probability or silently selected timing intent.
    hypotheses: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "kind": self.kind,
            "constraint_type": self.constraint_type,
            "status": self.status.value,
            "decision": self.decision.value,
            "rule_ids": list(self.rule_ids),
            "analysis": self.analysis,
            "source_objects": list(self.source_objects),
            "clock_refs": list(self.clock_refs),
            "port_refs": list(self.port_refs),
            "register_refs": list(self.register_refs),
            "evidence": [item.to_dict() for item in sorted(self.evidence, key=lambda item: item.id)],
            "provenance": self.provenance.to_dict(),
            "rationale": self.rationale,
            "source_snapshot_identity": dict(sorted(self.source_snapshot_identity.items())),
            "candidate_semantic_identity": self.candidate_semantic_identity,
            "constraint_template": copy.deepcopy(self.constraint_template),
            "constraint_templates": [copy.deepcopy(item) for item in self.constraint_templates],
            "knowledge_references": [dict(item) for item in sorted(
                self.knowledge_references,
                key=lambda item: (str(item.get("knowledge_item_id", "")), str(item.get("suggestion_id", ""))),
            )],
            "assumptions": sorted(self.assumptions),
            "warnings": sorted(self.warnings),
            "missing_information": [dict(item) for item in sorted(
                self.missing_information, key=lambda item: str(item.get("id", "")),
            )],
            "existing_constraint_ids": sorted(self.existing_constraint_ids),
            "acceptance_state": "NOT_ACCEPTED",
        }
        # Preserve established wire format for candidates with no new
        # ambiguity material, while exposing all nonempty hypotheses.
        if self.hypotheses:
            data["hypotheses"] = [copy.deepcopy(item) for item in sorted(
                self.hypotheses, key=lambda item: str(item.get("id", "")))]
        return data


@dataclass(frozen=True)
class InferenceAcceptanceResult:
    """Result of an explicit candidate-to-UCM conversion attempt."""

    status: str
    candidate_id: str
    constraint_id: str | None = None
    duplicate_of: str | None = None
    conflict_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    validation_issues: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_id": self.candidate_id,
            "constraint_id": self.constraint_id,
            "duplicate_of": self.duplicate_of,
            "conflict_ids": list(self.conflict_ids),
            "warnings": list(self.warnings),
            "validation_issues": [dict(item) for item in self.validation_issues],
        }


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
