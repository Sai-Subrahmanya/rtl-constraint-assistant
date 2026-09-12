"""Typed, deterministic reporting models for Step-29 constraint readiness.

These models describe an orchestration result.  They do not represent another
constraint model, validation result, coverage calculation, or provenance store.
Each field points back to the existing UCM, validation, coverage, inference,
application, MCMM, or preflight authority from which it was summarized.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ReadinessStatus(str, Enum):
    """Readiness assessment state; deliberately distinct from validation/QoR."""

    READY = "READY"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    BLOCKED = "BLOCKED"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class ReadinessSeverity(str, Enum):
    """Effect of a finding on the configured next-stage readiness decision."""

    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    INFORMATION = "INFORMATION"


class EvidenceState(str, Enum):
    """Categorical evidence strength without fabricated numerical confidence."""

    AUTHORITATIVE = "AUTHORITATIVE"
    VALIDATED = "VALIDATED"
    STRUCTURAL = "STRUCTURAL"
    INFERRED = "INFERRED"
    USER_PROVIDED = "USER_PROVIDED"
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"
    MISSING = "MISSING"


@dataclass(frozen=True)
class ReadinessEvidence:
    """A deterministic pointer to evidence owned by an existing subsystem."""

    state: EvidenceState
    source: str
    reference_id: str = ""
    detail: str = ""
    scenario_id: str | None = None
    snapshot_identity: str | None = None
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "source": self.source,
            "reference_id": self.reference_id or None,
            "detail": self.detail,
            "scenario_id": self.scenario_id,
            "snapshot_identity": self.snapshot_identity,
            "stale": self.stale,
        }


@dataclass(frozen=True)
class ReadinessRequirement:
    """One configuration-aware requirement summarized from existing evidence."""

    id: str
    category: str
    title: str
    status: ReadinessStatus
    required: bool
    rationale: str
    evidence: tuple[ReadinessEvidence, ...] = ()
    finding_ids: tuple[str, ...] = ()
    scenario_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "status": self.status.value,
            "required": self.required,
            "rationale": self.rationale,
            "scenario_id": self.scenario_id,
            "evidence": [item.to_dict() for item in sorted(
                self.evidence, key=lambda item: (item.source, item.reference_id, item.detail),
            )],
            "finding_ids": sorted(self.finding_ids),
        }


@dataclass(frozen=True)
class ReadinessFinding:
    """Actionable explanation of a requirement state without changing intent."""

    id: str
    requirement_id: str
    category: str
    severity: ReadinessSeverity
    status: ReadinessStatus
    message: str
    affected_object: str | None = None
    scenario_id: str | None = None
    evidence: tuple[ReadinessEvidence, ...] = ()
    missing_information: tuple[str, ...] = ()
    suggested_action: str | None = None
    provenance_references: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requirement_id": self.requirement_id,
            "category": self.category,
            "severity": self.severity.value,
            "status": self.status.value,
            "message": self.message,
            "affected_object": self.affected_object,
            "scenario_id": self.scenario_id,
            "evidence": [item.to_dict() for item in sorted(
                self.evidence, key=lambda item: (item.source, item.reference_id, item.detail),
            )],
            "missing_information": sorted(self.missing_information),
            "suggested_action": self.suggested_action,
            "provenance_references": sorted(self.provenance_references),
        }


@dataclass(frozen=True)
class ReadinessBlocker:
    """A typed, filtered view of a blocker finding for next-action consumers."""

    id: str
    requirement_id: str
    category: str
    status: ReadinessStatus
    message: str
    affected_object: str | None = None
    scenario_id: str | None = None
    evidence: tuple[ReadinessEvidence, ...] = ()
    suggested_action: str | None = None

    @classmethod
    def from_finding(cls, finding: ReadinessFinding) -> ReadinessBlocker:
        return cls(
            id=finding.id,
            requirement_id=finding.requirement_id,
            category=finding.category,
            status=finding.status,
            message=finding.message,
            affected_object=finding.affected_object,
            scenario_id=finding.scenario_id,
            evidence=finding.evidence,
            suggested_action=finding.suggested_action,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "requirement_id": self.requirement_id,
            "category": self.category,
            "status": self.status.value,
            "message": self.message,
            "affected_object": self.affected_object,
            "scenario_id": self.scenario_id,
            "evidence": [item.to_dict() for item in sorted(
                self.evidence, key=lambda item: (item.source, item.reference_id, item.detail),
            )],
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True)
class ScenarioReadinessResult:
    """Per-active-scenario view; never replaces the aggregate MCMM result."""

    scenario_id: str
    mode: str
    corner: str
    status: ReadinessStatus
    requirement_ids: tuple[str, ...] = ()
    blocker_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    coverage_scope: str = "aggregate_authoritative"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "mode": self.mode,
            "corner": self.corner,
            "status": self.status.value,
            "requirement_ids": sorted(self.requirement_ids),
            "blocker_ids": sorted(self.blocker_ids),
            "finding_ids": sorted(self.finding_ids),
            "coverage_scope": self.coverage_scope,
        }


_SEVERITY_ORDER = {
    ReadinessSeverity.BLOCKER: 0,
    ReadinessSeverity.WARNING: 1,
    ReadinessSeverity.INFORMATION: 2,
}


@dataclass(frozen=True)
class ConstraintReadinessReport:
    """Read-only Step-29 synthesis of authoritative readiness evidence."""

    status: ReadinessStatus
    requirements: tuple[ReadinessRequirement, ...]
    blockers: tuple[ReadinessBlocker, ...] = ()
    findings: tuple[ReadinessFinding, ...] = ()
    scenario_results: tuple[ScenarioReadinessResult, ...] = ()
    validation_summary: dict[str, Any] = field(default_factory=dict)
    coverage_summary: dict[str, Any] = field(default_factory=dict)
    evidence_summary: dict[str, Any] = field(default_factory=dict)
    stale_evidence: tuple[ReadinessFinding, ...] = ()
    next_actions: tuple[str, ...] = ()
    provenance_references: tuple[str, ...] = ()
    source_snapshot_identity: dict[str, str] = field(default_factory=dict)
    schema_version: int = 1
    kind: str = "rca_constraint_readiness"

    @property
    def warnings(self) -> tuple[ReadinessFinding, ...]:
        return tuple(item for item in self.findings if item.severity == ReadinessSeverity.WARNING)

    @property
    def informational_findings(self) -> tuple[ReadinessFinding, ...]:
        return tuple(item for item in self.findings if item.severity == ReadinessSeverity.INFORMATION)

    def to_dict(self) -> dict[str, Any]:
        findings = sorted(self.findings, key=lambda item: (
            _SEVERITY_ORDER[item.severity], item.requirement_id, item.scenario_id or "",
            item.affected_object or "", item.id,
        ))
        blockers = sorted(self.blockers, key=lambda item: (
            item.requirement_id, item.scenario_id or "", item.affected_object or "", item.id,
        ))
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "requirements": [item.to_dict() for item in sorted(
                self.requirements, key=lambda item: (item.id, item.scenario_id or ""),
            )],
            "blockers": [item.to_dict() for item in blockers],
            "warnings": [item.to_dict() for item in findings if item.severity == ReadinessSeverity.WARNING],
            "informational_findings": [item.to_dict() for item in findings
                                         if item.severity == ReadinessSeverity.INFORMATION],
            "findings": [item.to_dict() for item in findings],
            "scenario_results": [item.to_dict() for item in sorted(
                self.scenario_results, key=lambda item: item.scenario_id,
            )],
            "validation_summary": _stable_dict(self.validation_summary),
            "coverage_summary": _stable_dict(self.coverage_summary),
            "evidence_summary": _stable_dict(self.evidence_summary),
            "stale_evidence": [item.to_dict() for item in sorted(
                self.stale_evidence, key=lambda item: (item.requirement_id, item.scenario_id or "", item.id),
            )],
            "next_actions": sorted(set(self.next_actions)),
            "provenance_references": sorted(set(self.provenance_references)),
            "source_snapshot_identity": dict(sorted(self.source_snapshot_identity.items())),
        }


def _stable_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Sort one report-only mapping recursively for stable JSON rendering."""
    out: dict[str, Any] = {}
    for key in sorted(value, key=str):
        item = value[key]
        if isinstance(item, dict):
            out[str(key)] = _stable_dict(item)
        elif isinstance(item, (list, tuple)):
            out[str(key)] = [
                _stable_dict(part) if isinstance(part, dict) else part for part in item
            ]
        else:
            out[str(key)] = item
    return out


__all__ = [
    "ConstraintReadinessReport",
    "EvidenceState",
    "ReadinessBlocker",
    "ReadinessEvidence",
    "ReadinessFinding",
    "ReadinessRequirement",
    "ReadinessSeverity",
    "ReadinessStatus",
    "ScenarioReadinessResult",
]
