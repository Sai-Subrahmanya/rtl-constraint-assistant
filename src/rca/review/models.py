"""Typed, deterministic governance records for Step 31 constraint review.

These immutable records reference canonical UCM, readiness, validation,
coverage, formal, and lineage evidence owned by their existing subsystems. They
are deliberately not a second UCM, provenance store, history database, or EDA
signoff model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConstraintReviewStatus(str, Enum):
    """Current review-governance state, deliberately distinct from signoff."""

    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    APPROVED_WITH_WARNINGS = "APPROVED_WITH_WARNINGS"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    STALE = "STALE"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    INCOMPLETE = "INCOMPLETE"
    UNSUPPORTED = "UNSUPPORTED"
    REVOKED = "REVOKED"


class ReviewDecisionKind(str, Enum):
    """Explicit reviewer actions; assessment alone never selects one."""

    APPROVE = "APPROVE"
    APPROVE_WITH_WARNINGS = "APPROVE_WITH_WARNINGS"
    REJECT = "REJECT"
    DEFER = "DEFER"
    REVOKE = "REVOKE"


class ReviewFindingSeverity(str, Enum):
    """Effect of supplied evidence on the review decision boundary."""

    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    INFORMATION = "INFORMATION"


class ExternalEDASignoffStatus(str, Enum):
    """External signoff state; RCA approval never upgrades this state."""

    UNKNOWN = "EXTERNAL_EDA_SIGNOFF_UNKNOWN"
    SUPPLIED_EVIDENCE = "EXTERNAL_EDA_SIGNOFF_EVIDENCE_SUPPLIED"


@dataclass(frozen=True)
class ReviewActor:
    """Explicit reviewer identity, or a deliberately unspecified identity."""

    identity: str = "UNSPECIFIED"
    role: str = "UNSPECIFIED"

    @classmethod
    def from_value(cls, value: str | None, *, role: str | None = None) -> ReviewActor:
        return cls(identity=(value or "UNSPECIFIED").strip() or "UNSPECIFIED",
                   role=(role or "UNSPECIFIED").strip() or "UNSPECIFIED")

    def to_dict(self) -> dict[str, str]:
        return {"identity": self.identity, "role": self.role}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ReviewActor:
        data = data or {}
        return cls.from_value(str(data.get("identity") or "UNSPECIFIED"),
                              role=str(data.get("role") or "UNSPECIFIED"))


@dataclass(frozen=True)
class ReviewPolicy:
    """Explicit vendor-neutral review gates. Conservative defaults fail closed."""

    require_readiness: bool = True
    require_readiness_ready: bool = True
    require_validation_evidence: bool = True
    require_coverage_complete: bool = True
    require_all_active_scenarios: bool = True
    allow_approval_with_warnings: bool = False
    require_formal_for_constraint_types: tuple[str, ...] = ()
    allowed_unresolved_evidence_categories: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_readiness": self.require_readiness,
            "require_readiness_ready": self.require_readiness_ready,
            "require_validation_evidence": self.require_validation_evidence,
            "require_coverage_complete": self.require_coverage_complete,
            "require_all_active_scenarios": self.require_all_active_scenarios,
            "allow_approval_with_warnings": self.allow_approval_with_warnings,
            "require_formal_for_constraint_types": sorted(set(self.require_formal_for_constraint_types)),
            "allowed_unresolved_evidence_categories": sorted(set(self.allowed_unresolved_evidence_categories)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ReviewPolicy:
        data = data or {}
        return cls(
            require_readiness=bool(data.get("require_readiness", True)),
            require_readiness_ready=bool(data.get("require_readiness_ready", True)),
            require_validation_evidence=bool(data.get("require_validation_evidence", True)),
            require_coverage_complete=bool(data.get("require_coverage_complete", True)),
            require_all_active_scenarios=bool(data.get("require_all_active_scenarios", True)),
            allow_approval_with_warnings=bool(data.get("allow_approval_with_warnings", False)),
            require_formal_for_constraint_types=tuple(sorted({str(item) for item in
                data.get("require_formal_for_constraint_types", ()) if item})),
            allowed_unresolved_evidence_categories=tuple(sorted({str(item) for item in
                data.get("allowed_unresolved_evidence_categories", ()) if item})),
        )


@dataclass(frozen=True)
class ReviewScope:
    """MCMM-aware review scope; no scenario is silently broadened."""

    scope_kind: str
    requested_scenario_ids: tuple[str, ...] = ()
    active_scenario_ids: tuple[str, ...] = ()
    reviewed_scenario_ids: tuple[str, ...] = ()
    unknown_scenario_ids: tuple[str, ...] = ()
    scenario_definition_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_kind": self.scope_kind,
            "requested_scenario_ids": sorted(set(self.requested_scenario_ids)),
            "active_scenario_ids": sorted(set(self.active_scenario_ids)),
            "reviewed_scenario_ids": sorted(set(self.reviewed_scenario_ids)),
            "unknown_scenario_ids": sorted(set(self.unknown_scenario_ids)),
            "scenario_definition_identity": self.scenario_definition_identity or None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewScope:
        return cls(
            scope_kind=str(data.get("scope_kind") or "GLOBAL"),
            requested_scenario_ids=_strings(data.get("requested_scenario_ids")),
            active_scenario_ids=_strings(data.get("active_scenario_ids")),
            reviewed_scenario_ids=_strings(data.get("reviewed_scenario_ids")),
            unknown_scenario_ids=_strings(data.get("unknown_scenario_ids")),
            scenario_definition_identity=str(data.get("scenario_definition_identity") or ""),
        )


@dataclass(frozen=True)
class ReviewSnapshot:
    """References to exact existing UCM/evidence identities at review time."""

    ucm_content_identity: str
    ucm_semantic_identity: str
    lineage_snapshot_identity: str = ""
    lineage_report_identity: str = ""
    readiness_evidence_identity: str = ""
    validation_evidence_identity: str = ""
    coverage_evidence_identity: str = ""
    formal_evidence_identity: str = ""
    configuration_identity: str = ""
    design_identity: str = ""
    timing_graph_identity: str = ""
    constraint_ids: tuple[str, ...] = ()
    scenario_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ucm_content_identity": self.ucm_content_identity,
            "ucm_semantic_identity": self.ucm_semantic_identity,
            "lineage_snapshot_identity": self.lineage_snapshot_identity or None,
            "lineage_report_identity": self.lineage_report_identity or None,
            "readiness_evidence_identity": self.readiness_evidence_identity or None,
            "validation_evidence_identity": self.validation_evidence_identity or None,
            "coverage_evidence_identity": self.coverage_evidence_identity or None,
            "formal_evidence_identity": self.formal_evidence_identity or None,
            "configuration_identity": self.configuration_identity or None,
            "design_identity": self.design_identity or None,
            "timing_graph_identity": self.timing_graph_identity or None,
            "constraint_ids": sorted(set(self.constraint_ids)),
            "scenario_ids": sorted(set(self.scenario_ids)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewSnapshot:
        return cls(
            ucm_content_identity=str(data.get("ucm_content_identity") or ""),
            ucm_semantic_identity=str(data.get("ucm_semantic_identity") or ""),
            lineage_snapshot_identity=str(data.get("lineage_snapshot_identity") or ""),
            lineage_report_identity=str(data.get("lineage_report_identity") or ""),
            readiness_evidence_identity=str(data.get("readiness_evidence_identity") or ""),
            validation_evidence_identity=str(data.get("validation_evidence_identity") or ""),
            coverage_evidence_identity=str(data.get("coverage_evidence_identity") or ""),
            formal_evidence_identity=str(data.get("formal_evidence_identity") or ""),
            configuration_identity=str(data.get("configuration_identity") or ""),
            design_identity=str(data.get("design_identity") or ""),
            timing_graph_identity=str(data.get("timing_graph_identity") or ""),
            constraint_ids=_strings(data.get("constraint_ids")),
            scenario_ids=_strings(data.get("scenario_ids")),
        )


@dataclass(frozen=True)
class ReviewEvidenceRef:
    """Stable pointer to evidence still owned by an existing subsystem."""

    id: str
    authority: str
    reference_id: str
    status: str = "UNKNOWN"
    scenario_ids: tuple[str, ...] = ()
    stale: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "authority": self.authority,
            "reference_id": self.reference_id,
            "status": self.status,
            "scenario_ids": sorted(set(self.scenario_ids)),
            "stale": self.stale,
            "details": _stable_value(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewEvidenceRef:
        return cls(
            id=str(data.get("id") or ""), authority=str(data.get("authority") or "UNKNOWN"),
            reference_id=str(data.get("reference_id") or ""), status=str(data.get("status") or "UNKNOWN"),
            scenario_ids=_strings(data.get("scenario_ids")), stale=bool(data.get("stale", False)),
            details=dict(data.get("details") or {}),
        )


@dataclass(frozen=True)
class ReviewEvidence:
    """A categorized collection of references, never a copied evidence authority."""

    category: str
    status: str
    references: tuple[ReviewEvidenceRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "status": self.status,
            "references": [item.to_dict() for item in sorted(self.references, key=lambda item: item.id)],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewEvidence:
        return cls(
            category=str(data.get("category") or "UNKNOWN"), status=str(data.get("status") or "UNKNOWN"),
            references=tuple(sorted((ReviewEvidenceRef.from_dict(item) for item in data.get("references", ())
                                     if isinstance(item, Mapping)), key=lambda item: item.id)),
        )


@dataclass(frozen=True)
class ReviewFinding:
    """Informational or warning interpretation of supplied authoritative evidence."""

    id: str
    severity: ReviewFindingSeverity
    category: str
    message: str
    evidence_ref_ids: tuple[str, ...] = ()
    scenario_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity.value,
            "category": self.category,
            "message": self.message,
            "evidence_ref_ids": sorted(set(self.evidence_ref_ids)),
            "scenario_id": self.scenario_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewFinding:
        return cls(
            id=str(data.get("id") or ""),
            severity=ReviewFindingSeverity(str(data.get("severity") or "INFORMATION")),
            category=str(data.get("category") or "UNKNOWN"), message=str(data.get("message") or ""),
            evidence_ref_ids=_strings(data.get("evidence_ref_ids")),
            scenario_id=str(data["scenario_id"]) if data.get("scenario_id") is not None else None,
        )


@dataclass(frozen=True)
class ReviewBlocker:
    """A review finding that prevents an approval decision under the policy."""

    id: str
    category: str
    message: str
    evidence_ref_ids: tuple[str, ...] = ()
    scenario_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "message": self.message,
            "evidence_ref_ids": sorted(set(self.evidence_ref_ids)),
            "scenario_id": self.scenario_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewBlocker:
        return cls(
            id=str(data.get("id") or ""), category=str(data.get("category") or "UNKNOWN"),
            message=str(data.get("message") or ""), evidence_ref_ids=_strings(data.get("evidence_ref_ids")),
            scenario_id=str(data["scenario_id"]) if data.get("scenario_id") is not None else None,
        )


@dataclass(frozen=True)
class ReviewApproval:
    """Explicit reviewer decision. Its ID includes the supplied comment."""

    id: str
    kind: ReviewDecisionKind
    actor: ReviewActor
    comment: str
    review_id: str
    evidence_ref_ids: tuple[str, ...] = ()
    recorded_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "actor": self.actor.to_dict(),
            "comment": self.comment,
            "review_id": self.review_id,
            "evidence_ref_ids": sorted(set(self.evidence_ref_ids)),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewApproval:
        return cls(
            id=str(data.get("id") or ""), kind=ReviewDecisionKind(str(data.get("kind") or "DEFER")),
            actor=ReviewActor.from_dict(data.get("actor") if isinstance(data.get("actor"), Mapping) else None),
            comment=str(data.get("comment") or ""), review_id=str(data.get("review_id") or ""),
            evidence_ref_ids=_strings(data.get("evidence_ref_ids")),
            recorded_at=str(data["recorded_at"]) if data.get("recorded_at") is not None else None,
        )


@dataclass(frozen=True)
class ConstraintReview:
    """One immutable review-record revision for an exact canonical UCM snapshot."""

    id: str
    review_root_id: str
    reviewed_snapshot: ReviewSnapshot
    scope: ReviewScope
    policy: ReviewPolicy
    status: ConstraintReviewStatus = ConstraintReviewStatus.NEEDS_REVIEW
    approval: ReviewApproval | None = None
    decision_history: tuple[ReviewApproval, ...] = ()
    evidence: tuple[ReviewEvidence, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    blockers: tuple[ReviewBlocker, ...] = ()
    assumptions: tuple[str, ...] = ()
    previous_review_id: str | None = None
    supersedes_review_id: str | None = None
    external_eda_signoff: ExternalEDASignoffStatus = ExternalEDASignoffStatus.UNKNOWN
    external_eda_evidence_refs: tuple[str, ...] = ()
    schema_version: int = 1
    kind: str = "rca_constraint_review"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "id": self.id,
            "review_root_id": self.review_root_id,
            "reviewed_snapshot": self.reviewed_snapshot.to_dict(),
            "scope": self.scope.to_dict(),
            "policy": self.policy.to_dict(),
            "status": self.status.value,
            "approval": self.approval.to_dict() if self.approval is not None else None,
            "decision_history": [item.to_dict() for item in self.decision_history],
            "evidence": [item.to_dict() for item in sorted(self.evidence, key=lambda item: item.category)],
            "findings": [item.to_dict() for item in _sorted_findings(self.findings)],
            "blockers": [item.to_dict() for item in sorted(self.blockers, key=lambda item: item.id)],
            "assumptions": sorted(set(self.assumptions)),
            "previous_review_id": self.previous_review_id,
            "supersedes_review_id": self.supersedes_review_id,
            "external_eda_signoff": self.external_eda_signoff.value,
            "external_eda_evidence_refs": sorted(set(self.external_eda_evidence_refs)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConstraintReview:
        approval_data = data.get("approval")
        return cls(
            id=str(data.get("id") or ""), review_root_id=str(data.get("review_root_id") or data.get("id") or ""),
            reviewed_snapshot=ReviewSnapshot.from_dict(_mapping(data.get("reviewed_snapshot"))),
            scope=ReviewScope.from_dict(_mapping(data.get("scope"))), policy=ReviewPolicy.from_dict(_mapping(data.get("policy"))),
            status=ConstraintReviewStatus(str(data.get("status") or "NEEDS_REVIEW")),
            approval=(ReviewApproval.from_dict(approval_data) if isinstance(approval_data, Mapping) else None),
            decision_history=tuple(ReviewApproval.from_dict(item) for item in data.get("decision_history", ())
                                   if isinstance(item, Mapping)),
            evidence=tuple(sorted((ReviewEvidence.from_dict(item) for item in data.get("evidence", ())
                                   if isinstance(item, Mapping)), key=lambda item: item.category)),
            findings=tuple(_sorted_findings(tuple(ReviewFinding.from_dict(item) for item in data.get("findings", ())
                                                  if isinstance(item, Mapping)))),
            blockers=tuple(sorted((ReviewBlocker.from_dict(item) for item in data.get("blockers", ())
                                   if isinstance(item, Mapping)), key=lambda item: item.id)),
            assumptions=_strings(data.get("assumptions")),
            previous_review_id=str(data["previous_review_id"]) if data.get("previous_review_id") else None,
            supersedes_review_id=str(data["supersedes_review_id"]) if data.get("supersedes_review_id") else None,
            external_eda_signoff=ExternalEDASignoffStatus(str(data.get("external_eda_signoff")
                                                             or ExternalEDASignoffStatus.UNKNOWN.value)),
            external_eda_evidence_refs=_strings(data.get("external_eda_evidence_refs")),
            schema_version=int(data.get("schema_version", 1)), kind=str(data.get("kind") or "rca_constraint_review"),
        )


@dataclass(frozen=True)
class ConstraintReviewAssessment:
    """Read-only current assessment of a review record against supplied evidence."""

    review: ConstraintReview
    current_snapshot: ReviewSnapshot
    current_status: ConstraintReviewStatus
    approval_possible: bool
    approval_with_warnings_possible: bool
    snapshot_current: bool
    staleness_reasons: tuple[str, ...] = ()
    evidence: tuple[ReviewEvidence, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    blockers: tuple[ReviewBlocker, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "rca_constraint_review_assessment",
            "schema_version": 1,
            "review": self.review.to_dict(),
            "current_snapshot": self.current_snapshot.to_dict(),
            "current_status": self.current_status.value,
            "approval_possible": self.approval_possible,
            "approval_with_warnings_possible": self.approval_with_warnings_possible,
            "snapshot_current": self.snapshot_current,
            "staleness_reasons": sorted(set(self.staleness_reasons)),
            "evidence": [item.to_dict() for item in sorted(self.evidence, key=lambda item: item.category)],
            "findings": [item.to_dict() for item in _sorted_findings(self.findings)],
            "blockers": [item.to_dict() for item in sorted(self.blockers, key=lambda item: item.id)],
        }


# Concise public aliases for the conceptual review-decision/status names.
ConstraintReviewDecision = ReviewApproval
ReviewStatus = ConstraintReviewStatus


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({str(item) for item in value if item}))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sorted_findings(values: tuple[ReviewFinding, ...] | list[ReviewFinding]) -> list[ReviewFinding]:
    order = {ReviewFindingSeverity.BLOCKER: 0, ReviewFindingSeverity.WARNING: 1,
             ReviewFindingSeverity.INFORMATION: 2}
    return sorted(values, key=lambda item: (order[item.severity], item.category, item.scenario_id or "", item.id))


def _stable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
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
    "ConstraintReview",
    "ConstraintReviewAssessment",
    "ConstraintReviewDecision",
    "ConstraintReviewStatus",
    "ExternalEDASignoffStatus",
    "ReviewActor",
    "ReviewApproval",
    "ReviewBlocker",
    "ReviewDecisionKind",
    "ReviewEvidence",
    "ReviewEvidenceRef",
    "ReviewFinding",
    "ReviewFindingSeverity",
    "ReviewPolicy",
    "ReviewScope",
    "ReviewSnapshot",
    "ReviewStatus",
]
