"""Typed deterministic release-package records for Step 32.

The release layer records governance over already-authoritative UCM, review,
readiness, lineage, validation, coverage, formal, and artifact evidence. It is
not another UCM, review engine, manifest/cache authority, or EDA signoff model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from ..review import ReviewActor


class ReleaseStatus(str, Enum):
    """Release governance state, deliberately separate from EDA signoff."""

    CANDIDATE = "CANDIDATE"
    READY = "READY"
    RELEASED = "RELEASED"
    RELEASED_WITH_WARNINGS = "RELEASED_WITH_WARNINGS"
    BLOCKED = "BLOCKED"
    INVALID = "INVALID"
    STALE = "STALE"
    REVOKED = "REVOKED"
    UNKNOWN = "UNKNOWN"


class ReleaseIssueSeverity(str, Enum):
    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    INFORMATION = "INFORMATION"


class ReleaseDecisionKind(str, Enum):
    RELEASE = "RELEASE"
    REVOKE = "REVOKE"


class ReleaseLifecycleAction(str, Enum):
    """Explicit actions; assessment and verification are intentionally read-only."""

    ASSESS = "ASSESS"
    CREATE_CANDIDATE = "CREATE_CANDIDATE"
    RELEASE = "RELEASE"
    PACKAGE = "PACKAGE"
    VERIFY = "VERIFY"
    REVOKE = "REVOKE"
    SUPERSEDE = "SUPERSEDE"


class PackageVerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    INVALID = "INVALID"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class ExternalEDASignoffStatus(str, Enum):
    """Never upgraded by RCA release lifecycle state."""

    UNKNOWN = "EXTERNAL_EDA_SIGNOFF_UNKNOWN"
    EVIDENCE_SUPPLIED = "EXTERNAL_EDA_SIGNOFF_EVIDENCE_SUPPLIED"


@dataclass(frozen=True)
class ReleasePolicy:
    """Explicit vendor-neutral release gates; defaults are conservative."""

    require_review: bool = True
    allow_review_with_warnings: bool = False
    require_configuration: bool = True
    require_design: bool = True
    require_timing_graph: bool = True
    require_readiness: bool = True
    required_readiness_statuses: tuple[str, ...] = ("READY",)
    require_validation_evidence: bool = True
    require_complete_coverage: bool = True
    require_lineage: bool = True
    require_all_active_scenarios: bool = True
    require_formal_for_constraint_types: tuple[str, ...] = ()
    require_sdc: bool = False
    required_artifact_kinds: tuple[str, ...] = ()
    allow_release_with_warnings: bool = False
    allowed_unresolved_evidence_categories: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_review": self.require_review,
            "allow_review_with_warnings": self.allow_review_with_warnings,
            "require_configuration": self.require_configuration,
            "require_design": self.require_design,
            "require_timing_graph": self.require_timing_graph,
            "require_readiness": self.require_readiness,
            "required_readiness_statuses": sorted(set(self.required_readiness_statuses)),
            "require_validation_evidence": self.require_validation_evidence,
            "require_complete_coverage": self.require_complete_coverage,
            "require_lineage": self.require_lineage,
            "require_all_active_scenarios": self.require_all_active_scenarios,
            "require_formal_for_constraint_types": sorted(set(self.require_formal_for_constraint_types)),
            "require_sdc": self.require_sdc,
            "required_artifact_kinds": sorted(set(self.required_artifact_kinds)),
            "allow_release_with_warnings": self.allow_release_with_warnings,
            "allowed_unresolved_evidence_categories": sorted(set(self.allowed_unresolved_evidence_categories)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ReleasePolicy:
        data = data or {}
        return cls(
            require_review=bool(data.get("require_review", True)),
            allow_review_with_warnings=bool(data.get("allow_review_with_warnings", False)),
            require_configuration=bool(data.get("require_configuration", True)),
            require_design=bool(data.get("require_design", True)),
            require_timing_graph=bool(data.get("require_timing_graph", True)),
            require_readiness=bool(data.get("require_readiness", True)),
            required_readiness_statuses=_strings(data.get("required_readiness_statuses", ("READY",))),
            require_validation_evidence=bool(data.get("require_validation_evidence", True)),
            require_complete_coverage=bool(data.get("require_complete_coverage", True)),
            require_lineage=bool(data.get("require_lineage", True)),
            require_all_active_scenarios=bool(data.get("require_all_active_scenarios", True)),
            require_formal_for_constraint_types=_strings(data.get("require_formal_for_constraint_types")),
            require_sdc=bool(data.get("require_sdc", False)),
            required_artifact_kinds=_strings(data.get("required_artifact_kinds")),
            allow_release_with_warnings=bool(data.get("allow_release_with_warnings", False)),
            allowed_unresolved_evidence_categories=_strings(data.get("allowed_unresolved_evidence_categories")),
        )


@dataclass(frozen=True)
class ReleaseScope:
    """Exact MCMM release scope. It never broadens reviewed scenario intent."""

    scope_kind: str
    requested_scenario_ids: tuple[str, ...] = ()
    active_scenario_ids: tuple[str, ...] = ()
    released_scenario_ids: tuple[str, ...] = ()
    unknown_scenario_ids: tuple[str, ...] = ()
    scenario_definition_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_kind": self.scope_kind,
            "requested_scenario_ids": sorted(set(self.requested_scenario_ids)),
            "active_scenario_ids": sorted(set(self.active_scenario_ids)),
            "released_scenario_ids": sorted(set(self.released_scenario_ids)),
            "unknown_scenario_ids": sorted(set(self.unknown_scenario_ids)),
            "scenario_definition_identity": self.scenario_definition_identity or None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseScope:
        return cls(
            scope_kind=str(data.get("scope_kind") or "GLOBAL"),
            requested_scenario_ids=_strings(data.get("requested_scenario_ids")),
            active_scenario_ids=_strings(data.get("active_scenario_ids")),
            released_scenario_ids=_strings(data.get("released_scenario_ids")),
            unknown_scenario_ids=_strings(data.get("unknown_scenario_ids")),
            scenario_definition_identity=str(data.get("scenario_definition_identity") or ""),
        )


@dataclass(frozen=True)
class ReleaseSnapshot:
    """References to exact authoritative inputs that a release consumed."""

    ucm_content_identity: str
    ucm_semantic_identity: str
    review_id: str = ""
    review_snapshot_identity: str = ""
    readiness_identity: str = ""
    validation_identity: str = ""
    coverage_identity: str = ""
    lineage_snapshot_identity: str = ""
    lineage_identity: str = ""
    formal_identity: str = ""
    configuration_identity: str = ""
    design_identity: str = ""
    timing_graph_identity: str = ""
    constraint_ids: tuple[str, ...] = ()
    scenario_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ucm_content_identity": self.ucm_content_identity,
            "ucm_semantic_identity": self.ucm_semantic_identity or None,
            "review_id": self.review_id or None,
            "review_snapshot_identity": self.review_snapshot_identity or None,
            "readiness_identity": self.readiness_identity or None,
            "validation_identity": self.validation_identity or None,
            "coverage_identity": self.coverage_identity or None,
            "lineage_snapshot_identity": self.lineage_snapshot_identity or None,
            "lineage_identity": self.lineage_identity or None,
            "formal_identity": self.formal_identity or None,
            "configuration_identity": self.configuration_identity or None,
            "design_identity": self.design_identity or None,
            "timing_graph_identity": self.timing_graph_identity or None,
            "constraint_ids": sorted(set(self.constraint_ids)),
            "scenario_ids": sorted(set(self.scenario_ids)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseSnapshot:
        return cls(
            ucm_content_identity=str(data.get("ucm_content_identity") or ""),
            ucm_semantic_identity=str(data.get("ucm_semantic_identity") or ""),
            review_id=str(data.get("review_id") or ""),
            review_snapshot_identity=str(data.get("review_snapshot_identity") or ""),
            readiness_identity=str(data.get("readiness_identity") or ""),
            validation_identity=str(data.get("validation_identity") or ""),
            coverage_identity=str(data.get("coverage_identity") or ""),
            lineage_snapshot_identity=str(data.get("lineage_snapshot_identity") or ""),
            lineage_identity=str(data.get("lineage_identity") or ""),
            formal_identity=str(data.get("formal_identity") or ""),
            configuration_identity=str(data.get("configuration_identity") or ""),
            design_identity=str(data.get("design_identity") or ""),
            timing_graph_identity=str(data.get("timing_graph_identity") or ""),
            constraint_ids=_strings(data.get("constraint_ids")),
            scenario_ids=_strings(data.get("scenario_ids")),
        )


@dataclass(frozen=True)
class ReleaseIdentity:
    """Stable identity summary; never a competing canonical UCM identity."""

    id: str
    ucm_content_identity: str
    ucm_semantic_identity: str
    review_id: str = ""
    scope_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ucm_content_identity": self.ucm_content_identity,
            "ucm_semantic_identity": self.ucm_semantic_identity or None,
            "review_id": self.review_id or None,
            "scope_identity": self.scope_identity or None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseIdentity:
        return cls(
            id=str(data.get("id") or ""), ucm_content_identity=str(data.get("ucm_content_identity") or ""),
            ucm_semantic_identity=str(data.get("ucm_semantic_identity") or ""),
            review_id=str(data.get("review_id") or ""), scope_identity=str(data.get("scope_identity") or ""),
        )


@dataclass(frozen=True)
class ReleaseBaseline:
    """Immutable release candidate baseline over an existing canonical UCM."""

    id: str
    snapshot: ReleaseSnapshot
    scope: ReleaseScope
    review_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "snapshot": self.snapshot.to_dict(), "scope": self.scope.to_dict(),
                "review_id": self.review_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseBaseline:
        return cls(id=str(data.get("id") or ""), snapshot=ReleaseSnapshot.from_dict(_mapping(data.get("snapshot"))),
                   scope=ReleaseScope.from_dict(_mapping(data.get("scope"))),
                   review_id=str(data["review_id"]) if data.get("review_id") else None)


@dataclass(frozen=True)
class ReleaseEvidence:
    """Reference to already-owned evidence, not a duplicate evidence system."""

    id: str
    authority: str
    reference_id: str
    status: str = "UNKNOWN"
    required: bool = False
    stale: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _freeze_mapping(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "authority": self.authority, "reference_id": self.reference_id,
                "status": self.status, "required": self.required, "stale": self.stale,
                "details": _stable_value(self.details)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseEvidence:
        return cls(id=str(data.get("id") or ""), authority=str(data.get("authority") or "UNKNOWN"),
                   reference_id=str(data.get("reference_id") or ""), status=str(data.get("status") or "UNKNOWN"),
                   required=bool(data.get("required", False)), stale=bool(data.get("stale", False)),
                   details=_mapping(data.get("details")))


@dataclass(frozen=True)
class ReleaseDependency:
    """One explicit release input/dependency identity."""

    kind: str
    identity: str
    required: bool = True
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "identity": self.identity, "required": self.required, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseDependency:
        return cls(kind=str(data.get("kind") or "UNKNOWN"), identity=str(data.get("identity") or ""),
                   required=bool(data.get("required", True)), detail=str(data.get("detail") or ""))


@dataclass(frozen=True)
class ReleaseIssue:
    """Deterministic policy finding. BLOCKER prevents an explicit release action."""

    id: str
    severity: ReleaseIssueSeverity
    category: str
    message: str
    evidence_ids: tuple[str, ...] = ()
    scenario_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "severity": self.severity.value, "category": self.category,
                "message": self.message, "evidence_ids": sorted(set(self.evidence_ids)),
                "scenario_id": self.scenario_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseIssue:
        return cls(id=str(data.get("id") or ""), severity=ReleaseIssueSeverity(str(data.get("severity") or "INFORMATION")),
                   category=str(data.get("category") or "UNKNOWN"), message=str(data.get("message") or ""),
                   evidence_ids=_strings(data.get("evidence_ids")),
                   scenario_id=str(data["scenario_id"]) if data.get("scenario_id") is not None else None)


@dataclass(frozen=True)
class ReleaseDecision:
    """Explicit release/revocation action. Comment is identity-bearing content."""

    id: str
    kind: ReleaseDecisionKind
    actor: ReviewActor
    comment: str
    release_root_id: str
    evidence_ids: tuple[str, ...] = ()
    recorded_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind.value, "actor": self.actor.to_dict(), "comment": self.comment,
                "release_root_id": self.release_root_id, "evidence_ids": sorted(set(self.evidence_ids)),
                "recorded_at": self.recorded_at}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseDecision:
        return cls(id=str(data.get("id") or ""), kind=ReleaseDecisionKind(str(data.get("kind") or "RELEASE")),
                   actor=ReviewActor.from_dict(_mapping(data.get("actor"))), comment=str(data.get("comment") or ""),
                   release_root_id=str(data.get("release_root_id") or ""), evidence_ids=_strings(data.get("evidence_ids")),
                   recorded_at=str(data["recorded_at"]) if data.get("recorded_at") is not None else None)


@dataclass(frozen=True)
class ConstraintRelease:
    """One immutable release record/revision over an exact canonical UCM snapshot."""

    id: str
    release_root_id: str
    identity: ReleaseIdentity
    baseline: ReleaseBaseline
    snapshot: ReleaseSnapshot
    scope: ReleaseScope
    policy: ReleasePolicy
    status: ReleaseStatus = ReleaseStatus.CANDIDATE
    decision: ReleaseDecision | None = None
    decision_history: tuple[ReleaseDecision, ...] = ()
    evidence: tuple[ReleaseEvidence, ...] = ()
    dependencies: tuple[ReleaseDependency, ...] = ()
    issues: tuple[ReleaseIssue, ...] = ()
    previous_release_id: str | None = None
    supersedes_release_id: str | None = None
    external_eda_signoff: ExternalEDASignoffStatus = ExternalEDASignoffStatus.UNKNOWN
    external_eda_evidence_refs: tuple[str, ...] = ()
    schema_version: int = 1
    kind: str = "rca_constraint_release"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "schema_version": self.schema_version, "id": self.id,
            "release_root_id": self.release_root_id, "identity": self.identity.to_dict(),
            "baseline": self.baseline.to_dict(), "snapshot": self.snapshot.to_dict(), "scope": self.scope.to_dict(),
            "policy": self.policy.to_dict(), "status": self.status.value,
            "decision": self.decision.to_dict() if self.decision else None,
            "decision_history": [item.to_dict() for item in self.decision_history],
            "evidence": [item.to_dict() for item in sorted(self.evidence, key=lambda item: item.id)],
            "dependencies": [item.to_dict() for item in sorted(self.dependencies, key=lambda item: (item.kind, item.identity))],
            "issues": [item.to_dict() for item in _sorted_issues(self.issues)],
            "previous_release_id": self.previous_release_id, "supersedes_release_id": self.supersedes_release_id,
            "external_eda_signoff": self.external_eda_signoff.value,
            "external_eda_evidence_refs": sorted(set(self.external_eda_evidence_refs)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConstraintRelease:
        decision = data.get("decision")
        return cls(
            id=str(data.get("id") or ""), release_root_id=str(data.get("release_root_id") or data.get("id") or ""),
            identity=ReleaseIdentity.from_dict(_mapping(data.get("identity"))),
            baseline=ReleaseBaseline.from_dict(_mapping(data.get("baseline"))),
            snapshot=ReleaseSnapshot.from_dict(_mapping(data.get("snapshot"))),
            scope=ReleaseScope.from_dict(_mapping(data.get("scope"))), policy=ReleasePolicy.from_dict(_mapping(data.get("policy"))),
            status=ReleaseStatus(str(data.get("status") or "CANDIDATE")),
            decision=ReleaseDecision.from_dict(decision) if isinstance(decision, Mapping) else None,
            decision_history=tuple(ReleaseDecision.from_dict(item) for item in data.get("decision_history", ())
                                   if isinstance(item, Mapping)),
            evidence=tuple(sorted((ReleaseEvidence.from_dict(item) for item in data.get("evidence", ())
                                   if isinstance(item, Mapping)), key=lambda item: item.id)),
            dependencies=tuple(sorted((ReleaseDependency.from_dict(item) for item in data.get("dependencies", ())
                                      if isinstance(item, Mapping)), key=lambda item: (item.kind, item.identity))),
            issues=tuple(_sorted_issues(tuple(ReleaseIssue.from_dict(item) for item in data.get("issues", ())
                                              if isinstance(item, Mapping)))),
            previous_release_id=str(data["previous_release_id"]) if data.get("previous_release_id") else None,
            supersedes_release_id=str(data["supersedes_release_id"]) if data.get("supersedes_release_id") else None,
            external_eda_signoff=ExternalEDASignoffStatus(str(data.get("external_eda_signoff")
                                                              or ExternalEDASignoffStatus.UNKNOWN.value)),
            external_eda_evidence_refs=_strings(data.get("external_eda_evidence_refs")),
            schema_version=int(data.get("schema_version", 1)), kind=str(data.get("kind") or "rca_constraint_release"),
        )


@dataclass(frozen=True)
class ReleaseAssessment:
    """Read-only current release eligibility/currentness report."""

    release: ConstraintRelease
    current_snapshot: ReleaseSnapshot
    current_status: ReleaseStatus
    release_possible: bool
    release_with_warnings_possible: bool
    snapshot_current: bool
    staleness_reasons: tuple[str, ...] = ()
    evidence: tuple[ReleaseEvidence, ...] = ()
    dependencies: tuple[ReleaseDependency, ...] = ()
    issues: tuple[ReleaseIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "rca_constraint_release_assessment", "schema_version": 1,
            "release": self.release.to_dict(), "current_snapshot": self.current_snapshot.to_dict(),
            "current_status": self.current_status.value, "release_possible": self.release_possible,
            "release_with_warnings_possible": self.release_with_warnings_possible,
            "snapshot_current": self.snapshot_current, "staleness_reasons": sorted(set(self.staleness_reasons)),
            "evidence": [item.to_dict() for item in sorted(self.evidence, key=lambda item: item.id)],
            "dependencies": [item.to_dict() for item in sorted(self.dependencies, key=lambda item: (item.kind, item.identity))],
            "issues": [item.to_dict() for item in _sorted_issues(self.issues)],
        }


@dataclass(frozen=True)
class ReleaseArtifact:
    """Hash-bound member of an explicitly written release package."""

    id: str
    kind: str
    relative_path: str
    sha256: str
    size_bytes: int
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "relative_path": self.relative_path,
                "sha256": self.sha256, "size_bytes": self.size_bytes, "required": self.required}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseArtifact:
        return cls(id=str(data.get("id") or ""), kind=str(data.get("kind") or "UNKNOWN"),
                   relative_path=str(data.get("relative_path") or ""), sha256=str(data.get("sha256") or ""),
                   size_bytes=int(data.get("size_bytes", 0)), required=bool(data.get("required", True)))


@dataclass(frozen=True)
class ReleasePackage:
    """Portable descriptor for an explicit release package directory."""

    id: str
    release_id: str
    release_identity: str
    snapshot: ReleaseSnapshot
    scope: ReleaseScope
    dependencies: tuple[ReleaseDependency, ...] = ()
    artifacts: tuple[ReleaseArtifact, ...] = ()
    root_path: str = ""
    schema_version: int = 1
    kind: str = "rca_constraint_release_package"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "schema_version": self.schema_version, "id": self.id,
            "release_id": self.release_id, "release_identity": self.release_identity,
            "snapshot": self.snapshot.to_dict(), "scope": self.scope.to_dict(),
            "dependencies": [item.to_dict() for item in sorted(self.dependencies, key=lambda item: (item.kind, item.identity))],
            "artifacts": [item.to_dict() for item in sorted(self.artifacts, key=lambda item: item.relative_path)],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, root_path: str = "") -> ReleasePackage:
        return cls(
            id=str(data.get("id") or ""), release_id=str(data.get("release_id") or ""),
            release_identity=str(data.get("release_identity") or ""),
            snapshot=ReleaseSnapshot.from_dict(_mapping(data.get("snapshot"))),
            scope=ReleaseScope.from_dict(_mapping(data.get("scope"))),
            dependencies=tuple(sorted((ReleaseDependency.from_dict(item) for item in data.get("dependencies", ())
                                       if isinstance(item, Mapping)), key=lambda item: (item.kind, item.identity))),
            artifacts=tuple(sorted((ReleaseArtifact.from_dict(item) for item in data.get("artifacts", ())
                                    if isinstance(item, Mapping)), key=lambda item: item.relative_path)),
            root_path=root_path, schema_version=int(data.get("schema_version", 1)),
            kind=str(data.get("kind") or "rca_constraint_release_package"),
        )


@dataclass(frozen=True)
class ReleaseVerification:
    """Stateless package-integrity result; never repairs or executes tools."""

    package_id: str
    status: PackageVerificationStatus
    verified_artifact_ids: tuple[str, ...] = ()
    issues: tuple[ReleaseIssue, ...] = ()
    release_id: str = ""
    snapshot_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "rca_constraint_release_verification", "schema_version": 1,
            "package_id": self.package_id, "release_id": self.release_id,
            "snapshot_identity": self.snapshot_identity or None, "status": self.status.value,
            "verified_artifact_ids": sorted(set(self.verified_artifact_ids)),
            "issues": [item.to_dict() for item in _sorted_issues(self.issues)],
        }


def _freeze_mapping(value: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    return MappingProxyType({str(key): _freeze_value(raw[key]) for key in sorted(raw, key=str)})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze_value(item) for item in value), key=repr))
    return value


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({str(item) for item in value if item}))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sorted_issues(values: tuple[ReleaseIssue, ...] | list[ReleaseIssue]) -> list[ReleaseIssue]:
    order = {ReleaseIssueSeverity.BLOCKER: 0, ReleaseIssueSeverity.WARNING: 1,
             ReleaseIssueSeverity.INFORMATION: 2}
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
    "ConstraintRelease",
    "ExternalEDASignoffStatus",
    "PackageVerificationStatus",
    "ReleaseArtifact",
    "ReleaseAssessment",
    "ReleaseBaseline",
    "ReleaseDecision",
    "ReleaseDecisionKind",
    "ReleaseDependency",
    "ReleaseEvidence",
    "ReleaseIdentity",
    "ReleaseIssue",
    "ReleaseIssueSeverity",
    "ReleaseLifecycleAction",
    "ReleasePackage",
    "ReleasePolicy",
    "ReleaseScope",
    "ReleaseSnapshot",
    "ReleaseStatus",
    "ReleaseVerification",
]
