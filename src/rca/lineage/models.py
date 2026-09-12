"""Typed, deterministic read-only traceability projections for Step 30.

Lineage records link identifiers and evidence already owned by canonical UCM,
provenance, validation, inference/application, readiness, and MCMM systems. No
model in this module is a second UCM, history, graph database, or provenance
store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..provenance import Evidence, ProvenanceRecord
from ..readiness import ConstraintReadinessReport


class LineageSource(str, Enum):
    """Observed source classification, distinct from UCM ``SourceKind``."""

    USER_PROVIDED = "USER_PROVIDED"
    IMPORTED = "IMPORTED"
    INFERRED = "INFERRED"
    EXPLICITLY_ACCEPTED = "EXPLICITLY_ACCEPTED"
    KNOWLEDGE_REFERENCED = "KNOWLEDGE_REFERENCED"
    SYSTEM_OBSERVED = "SYSTEM_OBSERVED"
    UNKNOWN = "UNKNOWN"


class LineageEventKind(str, Enum):
    """Read-only event vocabulary; events do not mutate lifecycle state."""

    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    VALIDATED = "VALIDATED"
    INVALIDATED = "INVALIDATED"
    MODIFIED = "MODIFIED"
    REMOVED = "REMOVED"
    BECAME_STALE = "BECAME_STALE"
    READINESS_CHANGED = "READINESS_CHANGED"
    APPLICATION_ATTEMPTED = "APPLICATION_ATTEMPTED"


class LineageChangeKind(str, Enum):
    """Semantic snapshot-diff classifications, not raw JSON/text changes."""

    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"
    UNCHANGED = "UNCHANGED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class LineageSnapshot:
    """Compact identity/reference to an authoritative canonical UCM snapshot."""

    snapshot_identity: str
    name: str
    constraint_ids: tuple[str, ...] = ()
    scenario_ids: tuple[str, ...] = ()
    schema_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_identity": self.snapshot_identity,
            "name": self.name,
            "constraint_ids": sorted(self.constraint_ids),
            "scenario_ids": sorted(self.scenario_ids),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class LineageScenarioScope:
    """Exact canonical scenario scope plus a non-mutating active projection."""

    scope_kind: str
    scenario_ids: tuple[str, ...] = ()
    applicable_scenario_ids: tuple[str, ...] = ()
    unknown_scenario_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_kind": self.scope_kind,
            "scenario_ids": sorted(self.scenario_ids),
            "applicable_scenario_ids": sorted(self.applicable_scenario_ids),
            "unknown_scenario_ids": sorted(self.unknown_scenario_ids),
        }


@dataclass(frozen=True)
class LineageEvent:
    """Deterministic traceability record referencing already-owned evidence."""

    id: str
    kind: LineageEventKind
    subject_kind: str
    subject_id: str
    message: str
    snapshot_identity: str
    constraint_id: str | None = None
    candidate_id: str | None = None
    application_id: str | None = None
    scenario_id: str | None = None
    current_canonical: bool = False
    stale: bool = False
    evidence_ids: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "message": self.message,
            "snapshot_identity": self.snapshot_identity,
            "constraint_id": self.constraint_id,
            "candidate_id": self.candidate_id,
            "application_id": self.application_id,
            "scenario_id": self.scenario_id,
            "current_canonical": self.current_canonical,
            "stale": self.stale,
            "evidence_ids": sorted(self.evidence_ids),
            "details": _stable_value(self.details),
        }


@dataclass(frozen=True)
class ConstraintLineage:
    """Read-only lineage projection for one currently canonical constraint."""

    constraint_id: str
    constraint_type: str
    semantic_identity: str | None
    source: LineageSource
    origin_source: LineageSource
    source_kind: str | None
    current_canonical: bool
    snapshot_identity: str
    scenario_scope: LineageScenarioScope
    provenance: ProvenanceRecord | None = None
    evidence: tuple[Evidence, ...] = ()
    candidate_id: str | None = None
    application_id: str | None = None
    knowledge_references: tuple[dict[str, Any], ...] = ()
    knowledge_conflicts: tuple[dict[str, Any], ...] = ()
    assumption_ids: tuple[str, ...] = ()
    dependency_ids: tuple[str, ...] = ()
    validation_events: tuple[LineageEvent, ...] = ()
    formal_events: tuple[LineageEvent, ...] = ()
    events: tuple[LineageEvent, ...] = ()
    linkage_gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "constraint_type": self.constraint_type,
            "semantic_identity": self.semantic_identity,
            "source": self.source.value,
            "origin_source": self.origin_source.value,
            "source_kind": self.source_kind,
            "current_canonical": self.current_canonical,
            "snapshot_identity": self.snapshot_identity,
            "scenario_scope": self.scenario_scope.to_dict(),
            "provenance": self.provenance.to_dict() if self.provenance is not None else None,
            "evidence": [item.to_dict() for item in sorted(self.evidence, key=lambda item: item.id)],
            "candidate_id": self.candidate_id,
            "application_id": self.application_id,
            "knowledge_references": [_stable_value(item) for item in sorted(
                self.knowledge_references,
                key=lambda item: (str(item.get("knowledge_item_id", "")), str(item.get("suggestion_id", ""))),
            )],
            "knowledge_conflicts": [_stable_value(item) for item in sorted(
                self.knowledge_conflicts, key=lambda item: _stable_value(item).__repr__(),
            )],
            "assumption_ids": sorted(self.assumption_ids),
            "dependency_ids": sorted(self.dependency_ids),
            "validation_events": [item.to_dict() for item in _sorted_events(self.validation_events)],
            "formal_events": [item.to_dict() for item in _sorted_events(self.formal_events)],
            "events": [item.to_dict() for item in _sorted_events(self.events)],
            "linkage_gaps": sorted(set(self.linkage_gaps)),
        }


@dataclass(frozen=True)
class LineageChange:
    """One semantic UCM/snapshot or MCMM scenario change.

    ``evidence`` contains existing canonical evidence from the before/after
    constraint when one exists; it is not independently stored or adjudicated.
    """

    id: str
    kind: LineageChangeKind
    subject_kind: str
    before_constraint_id: str | None
    after_constraint_id: str | None
    before_semantic_identity: str | None
    after_semantic_identity: str | None
    constraint_type: str | None
    before_scenario_scope: LineageScenarioScope | None = None
    after_scenario_scope: LineageScenarioScope | None = None
    semantic_field_changes: tuple[dict[str, Any], ...] = ()
    source_before: LineageSource | None = None
    source_after: LineageSource | None = None
    evidence: tuple[Evidence, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "subject_kind": self.subject_kind,
            "before_constraint_id": self.before_constraint_id,
            "after_constraint_id": self.after_constraint_id,
            "before_semantic_identity": self.before_semantic_identity,
            "after_semantic_identity": self.after_semantic_identity,
            "constraint_type": self.constraint_type,
            "before_scenario_scope": (self.before_scenario_scope.to_dict()
                                      if self.before_scenario_scope is not None else None),
            "after_scenario_scope": (self.after_scenario_scope.to_dict()
                                     if self.after_scenario_scope is not None else None),
            "semantic_field_changes": [_stable_value(item) for item in self.semantic_field_changes],
            "source_before": self.source_before.value if self.source_before is not None else None,
            "source_after": self.source_after.value if self.source_after is not None else None,
            "evidence": [item.to_dict() for item in sorted(self.evidence, key=lambda item: item.id)],
            "details": _stable_value(self.details),
        }


@dataclass(frozen=True)
class UCMChangeSet:
    """Read-only semantic comparison projection over two canonical UCMs."""

    before: LineageSnapshot
    after: LineageSnapshot
    changes: tuple[LineageChange, ...] = ()
    comparison: dict[str, Any] = field(default_factory=dict)
    readiness_events: tuple[LineageEvent, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "changes": [item.to_dict() for item in sorted(
                self.changes,
                key=lambda item: (item.subject_kind, item.kind.value,
                                  item.before_constraint_id or "", item.after_constraint_id or "", item.id),
            )],
            "comparison": _stable_value(self.comparison),
            "readiness_events": [item.to_dict() for item in _sorted_events(self.readiness_events)],
        }


@dataclass(frozen=True)
class ConstraintLineageReport:
    """Top-level lineage report for one UCM, optionally with a prior snapshot."""

    snapshot: LineageSnapshot
    constraints: tuple[ConstraintLineage, ...] = ()
    application_attempts: tuple[LineageEvent, ...] = ()
    advisory_candidates: tuple[LineageEvent, ...] = ()
    global_validation_events: tuple[LineageEvent, ...] = ()
    readiness: ConstraintReadinessReport | None = None
    change_set: UCMChangeSet | None = None
    schema_version: int = 1
    kind: str = "rca_constraint_lineage"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "snapshot": self.snapshot.to_dict(),
            "constraints": [item.to_dict() for item in sorted(
                self.constraints, key=lambda item: item.constraint_id,
            )],
            "application_attempts": [item.to_dict() for item in _sorted_events(self.application_attempts)],
            "advisory_candidates": [item.to_dict() for item in _sorted_events(self.advisory_candidates)],
            "global_validation_events": [item.to_dict() for item in _sorted_events(self.global_validation_events)],
            "readiness": self.readiness.to_dict() if self.readiness is not None else None,
            "change_set": self.change_set.to_dict() if self.change_set is not None else None,
        }


def _sorted_events(events: tuple[LineageEvent, ...]) -> list[LineageEvent]:
    return sorted(events, key=lambda item: (
        item.kind.value, item.subject_kind, item.constraint_id or "", item.candidate_id or "",
        item.application_id or "", item.subject_id, item.id,
    ))


def _stable_value(value: Any) -> Any:
    """Return deterministic JSON-safe display content without creating authority."""
    if isinstance(value, dict):
        return {str(key): _stable_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_stable_value(item) for item in value), key=repr)
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return _stable_value(enum_value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


__all__ = [
    "ConstraintLineage",
    "ConstraintLineageReport",
    "LineageChange",
    "LineageChangeKind",
    "LineageEvent",
    "LineageEventKind",
    "LineageScenarioScope",
    "LineageSnapshot",
    "LineageSource",
    "UCMChangeSet",
]
