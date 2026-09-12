"""Immutable, vendor-neutral downstream handoff records (Step 33).

A handoff is a projection of an already released Step-32 package. It does not
become another constraint source, release authority, artifact authority, or EDA
signoff claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class HandoffStatus(str, Enum):
    PACKAGE_READY = "PACKAGE_READY"
    HANDOFF_READY = "HANDOFF_READY"
    HANDOFF_PREPARED = "HANDOFF_PREPARED"
    HANDOFF_EXECUTED = "HANDOFF_EXECUTED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"
    EDA_SIGNOFF_CONFIRMED = "EDA_SIGNOFF_CONFIRMED"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class HandoffTarget(str, Enum):
    GENERIC = "GENERIC"
    OPENSTA_OPENROAD = "OPENSTA_OPENROAD"
    SYNOPSYS = "SYNOPSYS"
    CADENCE = "CADENCE"
    FUTURE_VENDOR = "FUTURE_VENDOR"


class HandoffIssueSeverity(str, Enum):
    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    INFORMATION = "INFORMATION"


@dataclass(frozen=True)
class HandoffPolicy:
    """Explicit target compatibility and evidence requirements; fail closed."""

    require_verified_package: bool = True
    require_released_record: bool = True
    require_configuration_identity: bool = True
    require_sdc: bool = False
    sdc_dialect: str = "UNKNOWN"
    required_artifact_kinds: tuple[str, ...] = ()
    allow_unknown_sdc_dialect: bool = False
    allow_warnings: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_verified_package": self.require_verified_package,
            "require_released_record": self.require_released_record,
            "require_configuration_identity": self.require_configuration_identity,
            "require_sdc": self.require_sdc,
            "sdc_dialect": self.sdc_dialect,
            "required_artifact_kinds": sorted(set(self.required_artifact_kinds)),
            "allow_unknown_sdc_dialect": self.allow_unknown_sdc_dialect,
            "allow_warnings": self.allow_warnings,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> HandoffPolicy:
        data = data or {}
        artifacts = data.get("required_artifact_kinds", ())
        return cls(
            require_verified_package=bool(data.get("require_verified_package", True)),
            require_released_record=bool(data.get("require_released_record", True)),
            require_configuration_identity=bool(data.get("require_configuration_identity", True)),
            require_sdc=bool(data.get("require_sdc", False)),
            sdc_dialect=str(data.get("sdc_dialect") or "UNKNOWN").upper(),
            required_artifact_kinds=tuple(sorted({str(item) for item in artifacts if item}))
            if isinstance(artifacts, (list, tuple, set, frozenset)) else (),
            allow_unknown_sdc_dialect=bool(data.get("allow_unknown_sdc_dialect", False)),
            allow_warnings=bool(data.get("allow_warnings", False)),
        )


@dataclass(frozen=True)
class HandoffScope:
    """Exact release scenario scope, copied without broadening or dropping IDs."""

    scope_kind: str
    requested_scenario_ids: tuple[str, ...] = ()
    active_scenario_ids: tuple[str, ...] = ()
    released_scenario_ids: tuple[str, ...] = ()
    scenario_definition_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_kind": self.scope_kind,
            "requested_scenario_ids": sorted(set(self.requested_scenario_ids)),
            "active_scenario_ids": sorted(set(self.active_scenario_ids)),
            "released_scenario_ids": sorted(set(self.released_scenario_ids)),
            "scenario_definition_identity": self.scenario_definition_identity or None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HandoffScope:
        return cls(
            scope_kind=str(data.get("scope_kind") or "GLOBAL"),
            requested_scenario_ids=_strings(data.get("requested_scenario_ids")),
            active_scenario_ids=_strings(data.get("active_scenario_ids")),
            released_scenario_ids=_strings(data.get("released_scenario_ids")),
            scenario_definition_identity=str(data.get("scenario_definition_identity") or ""),
        )


@dataclass(frozen=True)
class HandoffArtifact:
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
    def from_dict(cls, data: Mapping[str, Any]) -> HandoffArtifact:
        return cls(id=str(data.get("id") or ""), kind=str(data.get("kind") or "UNKNOWN"),
                   relative_path=str(data.get("relative_path") or ""), sha256=str(data.get("sha256") or ""),
                   size_bytes=int(data.get("size_bytes", 0)), required=bool(data.get("required", True)))


@dataclass(frozen=True)
class HandoffEvidence:
    id: str
    category: str
    reference_id: str
    status: str
    required: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _freeze_mapping(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "category": self.category, "reference_id": self.reference_id,
                "status": self.status, "required": self.required, "details": _stable_value(self.details)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HandoffEvidence:
        return cls(id=str(data.get("id") or ""), category=str(data.get("category") or "UNKNOWN"),
                   reference_id=str(data.get("reference_id") or ""), status=str(data.get("status") or "UNKNOWN"),
                   required=bool(data.get("required", False)), details=_mapping(data.get("details")))


@dataclass(frozen=True)
class HandoffDependency:
    kind: str
    identity: str
    required: bool = True
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "identity": self.identity, "required": self.required, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HandoffDependency:
        return cls(kind=str(data.get("kind") or "UNKNOWN"), identity=str(data.get("identity") or ""),
                   required=bool(data.get("required", True)), detail=str(data.get("detail") or ""))


@dataclass(frozen=True)
class HandoffIssue:
    id: str
    severity: HandoffIssueSeverity
    category: str
    message: str
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "severity": self.severity.value, "category": self.category,
                "message": self.message, "evidence_ids": sorted(set(self.evidence_ids))}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HandoffIssue:
        return cls(id=str(data.get("id") or ""),
                   severity=HandoffIssueSeverity(str(data.get("severity") or "INFORMATION")),
                   category=str(data.get("category") or "UNKNOWN"), message=str(data.get("message") or ""),
                   evidence_ids=_strings(data.get("evidence_ids")))


@dataclass(frozen=True)
class HandoffIdentity:
    """Deterministic identity over target plus exact verified release content."""

    id: str
    package_id: str
    release_id: str
    ucm_content_identity: str
    ucm_semantic_identity: str
    scope_identity: str
    configuration_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "package_id": self.package_id, "release_id": self.release_id,
                "ucm_content_identity": self.ucm_content_identity,
                "ucm_semantic_identity": self.ucm_semantic_identity or None,
                "scope_identity": self.scope_identity, "configuration_identity": self.configuration_identity or None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HandoffIdentity:
        return cls(id=str(data.get("id") or ""), package_id=str(data.get("package_id") or ""),
                   release_id=str(data.get("release_id") or ""),
                   ucm_content_identity=str(data.get("ucm_content_identity") or ""),
                   ucm_semantic_identity=str(data.get("ucm_semantic_identity") or ""),
                   scope_identity=str(data.get("scope_identity") or ""),
                   configuration_identity=str(data.get("configuration_identity") or ""))


@dataclass(frozen=True)
class ConstraintHandoff:
    """Prepared target projection; immutable and distinct from package/release authority."""

    id: str
    identity: HandoffIdentity
    target: HandoffTarget
    status: HandoffStatus
    policy: HandoffPolicy
    scope: HandoffScope
    package_path: str = ""
    artifacts: tuple[HandoffArtifact, ...] = ()
    evidence: tuple[HandoffEvidence, ...] = ()
    dependencies: tuple[HandoffDependency, ...] = ()
    issues: tuple[HandoffIssue, ...] = ()
    execution_evidence_refs: tuple[str, ...] = ()
    schema_version: int = 1
    kind: str = "rca_constraint_handoff"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "schema_version": self.schema_version, "id": self.id,
            "identity": self.identity.to_dict(), "target": self.target.value, "status": self.status.value,
            "policy": self.policy.to_dict(), "scope": self.scope.to_dict(),
            # Package path is an invocation locator and intentionally absent
            # from the handoff identity, never an engineering identifier.
            "package_path": self.package_path or None,
            "artifacts": [item.to_dict() for item in sorted(self.artifacts, key=lambda item: item.relative_path)],
            "evidence": [item.to_dict() for item in sorted(self.evidence, key=lambda item: item.id)],
            "dependencies": [item.to_dict() for item in sorted(self.dependencies, key=lambda item: (item.kind, item.identity))],
            "issues": [item.to_dict() for item in _sorted_issues(self.issues)],
            "execution_evidence_refs": sorted(set(self.execution_evidence_refs)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConstraintHandoff:
        return cls(
            id=str(data.get("id") or ""), identity=HandoffIdentity.from_dict(_mapping(data.get("identity"))),
            target=HandoffTarget(str(data.get("target") or HandoffTarget.GENERIC.value)),
            status=HandoffStatus(str(data.get("status") or HandoffStatus.UNKNOWN.value)),
            policy=HandoffPolicy.from_dict(_mapping(data.get("policy"))),
            scope=HandoffScope.from_dict(_mapping(data.get("scope"))), package_path=str(data.get("package_path") or ""),
            artifacts=tuple(sorted((HandoffArtifact.from_dict(item) for item in data.get("artifacts", ())
                                    if isinstance(item, Mapping)), key=lambda item: item.relative_path)),
            evidence=tuple(sorted((HandoffEvidence.from_dict(item) for item in data.get("evidence", ())
                                   if isinstance(item, Mapping)), key=lambda item: item.id)),
            dependencies=tuple(sorted((HandoffDependency.from_dict(item) for item in data.get("dependencies", ())
                                      if isinstance(item, Mapping)), key=lambda item: (item.kind, item.identity))),
            issues=tuple(_sorted_issues([HandoffIssue.from_dict(item) for item in data.get("issues", ())
                                          if isinstance(item, Mapping)])),
            execution_evidence_refs=_strings(data.get("execution_evidence_refs")),
            schema_version=int(data.get("schema_version", 1)), kind=str(data.get("kind") or "rca_constraint_handoff"),
        )


@dataclass(frozen=True)
class HandoffAssessment:
    handoff: ConstraintHandoff
    package_status: HandoffStatus
    handoff_possible: bool
    preparation_possible: bool
    issues: tuple[HandoffIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "rca_constraint_handoff_assessment", "schema_version": 1,
                "handoff": self.handoff.to_dict(), "package_status": self.package_status.value,
                "handoff_possible": self.handoff_possible, "preparation_possible": self.preparation_possible,
                "issues": [item.to_dict() for item in _sorted_issues(self.issues)]}


@dataclass(frozen=True)
class HandoffResult:
    handoff: ConstraintHandoff
    status: HandoffStatus
    executed: bool = False
    actual_external_tool_executed: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "rca_constraint_handoff_result", "schema_version": 1,
                "handoff": self.handoff.to_dict(), "status": self.status.value, "executed": self.executed,
                "actual_external_tool_executed": self.actual_external_tool_executed, "message": self.message}


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({str(item) for item in value if item}))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def _sorted_issues(values: list[HandoffIssue] | tuple[HandoffIssue, ...]) -> list[HandoffIssue]:
    rank = {HandoffIssueSeverity.BLOCKER: 0, HandoffIssueSeverity.WARNING: 1,
            HandoffIssueSeverity.INFORMATION: 2}
    return sorted(values, key=lambda item: (rank[item.severity], item.category, item.id))


__all__ = [
    "ConstraintHandoff", "HandoffArtifact", "HandoffAssessment", "HandoffDependency", "HandoffEvidence",
    "HandoffIdentity", "HandoffIssue", "HandoffIssueSeverity", "HandoffPolicy", "HandoffResult", "HandoffScope",
    "HandoffStatus", "HandoffTarget",
]
