"""Read-only downstream handoff assessment/preparation over Step-32 packages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..release import (
    ConstraintRelease,
    PackageVerificationStatus,
    ReleasePackage,
    verify_release_package,
)
from ..utils.hashing import hash_file, stable_hash
from .models import (
    ConstraintHandoff,
    HandoffArtifact,
    HandoffAssessment,
    HandoffDependency,
    HandoffEvidence,
    HandoffIdentity,
    HandoffIssue,
    HandoffIssueSeverity,
    HandoffPolicy,
    HandoffResult,
    HandoffScope,
    HandoffStatus,
    HandoffTarget,
)

_TARGET_DIALECTS: dict[HandoffTarget, frozenset[str]] = {
    HandoffTarget.GENERIC: frozenset({"GENERIC", "OPENSTA", "SYNOPSYS", "CADENCE", "UNKNOWN"}),
    HandoffTarget.OPENSTA_OPENROAD: frozenset({"GENERIC", "OPENSTA"}),
    HandoffTarget.SYNOPSYS: frozenset({"SYNOPSYS"}),
    HandoffTarget.CADENCE: frozenset({"CADENCE"}),
    HandoffTarget.FUTURE_VENDOR: frozenset(),
}


class HandoffError(ValueError):
    """Raised when explicit handoff preparation cannot safely proceed."""


def assess_constraint_handoff(
    package_dir: str | Path,
    *,
    target: HandoffTarget | str = HandoffTarget.GENERIC,
    policy: HandoffPolicy | None = None,
    config: Any | None = None,
) -> HandoffAssessment:
    """Read a package and assess a target without modifying either one.

    This does not render an SDC, rewrite package files, run tools, or assert
    EDA signoff. A package verifier is consumed as the package-integrity
    authority; release/UCM data are reconstructed only to check references.
    """
    target = _target(target)
    policy = policy or HandoffPolicy()
    root = Path(package_dir)
    verification = verify_release_package(root)
    package, release, parse_issue = _load_package_release(root)
    issues: list[HandoffIssue] = []
    evidence: list[HandoffEvidence] = []
    dependencies: list[HandoffDependency] = []

    def add(category: str, reference: str, status: str, *, required: bool = False,
            details: Mapping[str, Any] | None = None) -> HandoffEvidence:
        record = _evidence(category, reference, status, required=required, details=details)
        evidence.append(record)
        return record

    def finding(severity: HandoffIssueSeverity, category: str, message: str,
                refs: tuple[str, ...] = ()) -> None:
        issues.append(_issue(severity, category, message, refs))

    verification_ref = add("RELEASE_PACKAGE", verification.package_id or "UNREADABLE", verification.status.value,
                           required=policy.require_verified_package,
                           details={"verified_artifact_ids": list(verification.verified_artifact_ids)})
    if verification.status != PackageVerificationStatus.VERIFIED:
        severity = HandoffIssueSeverity.BLOCKER if policy.require_verified_package else HandoffIssueSeverity.WARNING
        finding(severity, "RELEASE_PACKAGE", "Release package integrity verification did not succeed.",
                (verification_ref.id,))
    if parse_issue is not None:
        finding(HandoffIssueSeverity.BLOCKER, "RELEASE_PACKAGE", parse_issue, (verification_ref.id,))

    if package is None or release is None:
        # Keep an honest explicitly incomplete handoff identity. It has no
        # release/UCM claim and cannot become prepared.
        empty_scope = HandoffScope("GLOBAL")
        identity = _identity("", "", "", "", empty_scope, target, policy, (), "")
        handoff = ConstraintHandoff(
            id=_handoff_id(identity, target, policy, (), (), issues), identity=identity, target=target,
            status=HandoffStatus.INVALID, policy=policy, scope=empty_scope, package_path=str(root),
            evidence=tuple(sorted(evidence, key=lambda item: item.id)), issues=tuple(_sorted_issues(issues)),
        )
        return HandoffAssessment(handoff, HandoffStatus.INVALID, False, False, handoff.issues)

    scope = HandoffScope(
        scope_kind=package.scope.scope_kind,
        requested_scenario_ids=package.scope.requested_scenario_ids,
        active_scenario_ids=package.scope.active_scenario_ids,
        released_scenario_ids=package.scope.released_scenario_ids,
        scenario_definition_identity=package.scope.scenario_definition_identity,
    )
    dependencies.extend(HandoffDependency(item.kind, item.identity, item.required, item.detail)
                        for item in package.dependencies)
    dependencies.append(HandoffDependency("release_package", package.id, True, "Verified Step-32 package descriptor."))
    release_ref = add("RELEASE", release.id, release.status.value, required=policy.require_released_record,
                      details={"release_identity": release.identity.id})
    ucm_ref = add("CANONICAL_UCM", package.snapshot.ucm_content_identity, "BOUND", required=True,
                  details={"semantic_identity": package.snapshot.ucm_semantic_identity or "UNKNOWN"})
    scope_ref = add("MCMM_SCOPE", scope.scenario_definition_identity or "UNKNOWN", "BOUND", required=True,
                    details=scope.to_dict())
    if policy.require_released_record and release.status.value not in {"RELEASED", "RELEASED_WITH_WARNINGS"}:
        finding(HandoffIssueSeverity.BLOCKER, "RELEASE", "Package does not contain an explicitly released record.",
                (release_ref.id,))
    if release.status.value == "REVOKED":
        finding(HandoffIssueSeverity.BLOCKER, "RELEASE", "A revoked release cannot be handed downstream.", (release_ref.id,))
    if not package.snapshot.ucm_content_identity or not package.snapshot.ucm_semantic_identity:
        finding(HandoffIssueSeverity.BLOCKER, "UCM", "Package has unknown/missing canonical or semantic UCM identity.",
                (ucm_ref.id,))
    if not scope.scenario_definition_identity or not scope.released_scenario_ids and scope.active_scenario_ids:
        finding(HandoffIssueSeverity.BLOCKER, "MCMM_SCOPE", "Package MCMM release scope is incomplete or ambiguous.",
                (scope_ref.id,))

    _configuration_gate(config, policy, add, dependencies, finding)
    supplied_configuration_identity = _identity_value(config)
    if (package.snapshot.configuration_identity and supplied_configuration_identity
            and package.snapshot.configuration_identity != supplied_configuration_identity):
        finding(HandoffIssueSeverity.BLOCKER, "CONFIGURATION",
                "Supplied handoff configuration identity differs from released package baseline.")
    artifacts = tuple(_artifact(item) for item in package.artifacts)
    _artifact_gate(package, root, target, policy, artifacts, add, dependencies, finding)
    _target_gate(target, policy, add, finding)

    blockers = any(item.severity == HandoffIssueSeverity.BLOCKER for item in issues)
    warnings = any(item.severity == HandoffIssueSeverity.WARNING for item in issues)
    package_ready = verification.status == PackageVerificationStatus.VERIFIED and not parse_issue
    if blockers or warnings and not policy.allow_warnings:
        status = HandoffStatus.FAILED
    elif target == HandoffTarget.FUTURE_VENDOR:
        status = HandoffStatus.UNSUPPORTED
    elif package_ready:
        status = HandoffStatus.HANDOFF_READY
    else:
        status = HandoffStatus.PACKAGE_READY
    configuration_identity = _identity_value(config)
    identity = _identity(package.id, release.id, package.snapshot.ucm_content_identity,
                         package.snapshot.ucm_semantic_identity, scope, target, policy, artifacts,
                         configuration_identity)
    handoff = ConstraintHandoff(
        id=_handoff_id(identity, target, policy, artifacts, dependencies, issues), identity=identity,
        target=target, status=status, policy=policy, scope=scope, package_path=str(root), artifacts=artifacts,
        evidence=tuple(sorted(evidence, key=lambda item: item.id)),
        dependencies=tuple(sorted(dependencies, key=lambda item: (item.kind, item.identity))),
        issues=tuple(_sorted_issues(issues)),
    )
    possible = status == HandoffStatus.HANDOFF_READY
    return HandoffAssessment(handoff, HandoffStatus.PACKAGE_READY if package_ready else HandoffStatus.INVALID,
                             possible, possible, handoff.issues)


def prepare_constraint_handoff(
    package_dir: str | Path,
    *,
    target: HandoffTarget | str = HandoffTarget.GENERIC,
    policy: HandoffPolicy | None = None,
    config: Any | None = None,
) -> ConstraintHandoff:
    """Return an explicit immutable prepared handoff; never copies/renders/mutates."""
    assessment = assess_constraint_handoff(package_dir, target=target, policy=policy, config=config)
    if not assessment.preparation_possible:
        raise HandoffError(_failure_message(assessment))
    return replace(assessment.handoff, status=HandoffStatus.HANDOFF_PREPARED)


def execute_constraint_handoff(handoff: ConstraintHandoff, *, execute: bool = False) -> HandoffResult:
    """Honest execution boundary, intentionally no hidden target-tool invocation.

    Step 33 exposes readiness/preparation only. A caller cannot turn supplied
    package text into a claimed tool run by toggling a flag: actual EDA execution
    remains the explicit existing Step-25/40 flow and must retain its manifest.
    """
    if handoff.status != HandoffStatus.HANDOFF_PREPARED:
        return HandoffResult(handoff, HandoffStatus.FAILED, message="Only a prepared handoff may reach execution boundary.")
    if not execute:
        return HandoffResult(handoff, HandoffStatus.HANDOFF_PREPARED, message="Execution was not requested.")
    unavailable = replace(handoff, status=HandoffStatus.UNAVAILABLE)
    return HandoffResult(
        unavailable, HandoffStatus.UNAVAILABLE, executed=False, actual_external_tool_executed=False,
        message="No external target execution was performed by handoff; use an explicitly configured existing EDA flow.",
    )


def verify_constraint_handoff(handoff: ConstraintHandoff | Mapping[str, Any], package_dir: str | Path | None = None) -> HandoffAssessment:
    """Statelessly re-assess serialized handoff identity against its release package."""
    record = ConstraintHandoff.from_dict(handoff) if isinstance(handoff, Mapping) else handoff
    root = Path(package_dir) if package_dir is not None else Path(record.package_path)
    assessment = assess_constraint_handoff(root, target=record.target, policy=record.policy)
    issues = list(assessment.issues)
    expected = assessment.handoff
    if record.identity != expected.identity or record.id != expected.id:
        issues.append(_issue(HandoffIssueSeverity.BLOCKER, "HANDOFF", "Serialized handoff identity differs from package/policy content."))
    if record.scope != expected.scope:
        issues.append(_issue(HandoffIssueSeverity.BLOCKER, "MCMM_SCOPE", "Serialized handoff scope differs from verified package scope."))
    if record.artifacts != expected.artifacts:
        issues.append(_issue(HandoffIssueSeverity.BLOCKER, "ARTIFACT", "Serialized handoff artifact list differs from verified package."))
    status = HandoffStatus.FAILED if any(item.severity == HandoffIssueSeverity.BLOCKER for item in issues) else expected.status
    verified = replace(expected, status=status, issues=tuple(_sorted_issues(issues)))
    possible = status == HandoffStatus.HANDOFF_READY
    return HandoffAssessment(verified, assessment.package_status, possible, possible, verified.issues)


@dataclass(frozen=True)
class ConstraintHandoffEngine:
    """Stateless handoff façade; it retains no release or execution authority."""

    def assess(self, package_dir: str | Path, **kwargs: Any) -> HandoffAssessment:
        return assess_constraint_handoff(package_dir, **kwargs)

    def prepare(self, package_dir: str | Path, **kwargs: Any) -> ConstraintHandoff:
        return prepare_constraint_handoff(package_dir, **kwargs)

    def verify(self, handoff: ConstraintHandoff | Mapping[str, Any], package_dir: str | Path | None = None) -> HandoffAssessment:
        return verify_constraint_handoff(handoff, package_dir)


def _load_package_release(root: Path) -> tuple[ReleasePackage | None, ConstraintRelease | None, str | None]:
    try:
        package_data = json.loads((root / "release_manifest.json").read_text(encoding="utf-8"))
        release_data = json.loads((root / "release.json").read_text(encoding="utf-8"))
        if not isinstance(package_data, Mapping) or not isinstance(release_data, Mapping):
            raise TypeError("release package records must be JSON objects")
        return ReleasePackage.from_dict(package_data, root_path=str(root)), ConstraintRelease.from_dict(release_data), None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, None, f"Cannot read release package records: {exc}"


def _configuration_gate(config: Any | None, policy: HandoffPolicy, add, dependencies, finding) -> None:
    identity = _identity_value(config)
    ref = add("CONFIGURATION", identity or "UNSUPPLIED", "BOUND" if identity else "MISSING",
              required=policy.require_configuration_identity)
    if identity:
        dependencies.append(HandoffDependency("configuration", identity, policy.require_configuration_identity,
                                               "Supplied downstream configuration identity."))
    elif policy.require_configuration_identity:
        finding(HandoffIssueSeverity.BLOCKER, "CONFIGURATION", "HandoffPolicy requires explicit configuration identity.", (ref.id,))


def _artifact_gate(package: ReleasePackage, root: Path, target: HandoffTarget, policy: HandoffPolicy,
                   artifacts: tuple[HandoffArtifact, ...], add, dependencies, finding) -> None:
    kinds = {artifact.kind for artifact in artifacts}
    for artifact in artifacts:
        path = _safe_path(root, artifact.relative_path)
        exists = bool(path and path.is_file())
        digest = hash_file(path) if exists and path is not None else ""
        ref = add("ARTIFACT", artifact.id, "HASHED" if exists and digest == artifact.sha256 else "MISSING_OR_CHANGED",
                  required=artifact.required, details={"kind": artifact.kind, "sha256": artifact.sha256})
        dependencies.append(HandoffDependency(f"artifact:{artifact.kind}", artifact.sha256, artifact.required,
                                               "Hash-bound release package artifact."))
        if artifact.required and (not exists or digest != artifact.sha256):
            finding(HandoffIssueSeverity.BLOCKER, "ARTIFACT", f"Required handoff artifact {artifact.relative_path} is missing or changed.",
                    (ref.id,))
    for kind in sorted(set(policy.required_artifact_kinds) - kinds):
        ref = add("ARTIFACT", kind, "MISSING", required=True)
        finding(HandoffIssueSeverity.BLOCKER, "ARTIFACT", f"HandoffPolicy requires artifact kind {kind!r}.", (ref.id,))
    if policy.require_sdc and "sdc" not in kinds:
        ref = add("SDC", "UNSUPPLIED", "MISSING", required=True)
        finding(HandoffIssueSeverity.BLOCKER, "SDC", "HandoffPolicy requires a pre-existing supplied SDC package artifact.", (ref.id,))
    if target != HandoffTarget.GENERIC and "sdc" not in kinds:
        ref = add("SDC", "UNSUPPLIED", "MISSING", required=True)
        finding(HandoffIssueSeverity.BLOCKER, "SDC", "Target handoff has no explicit supplied SDC; handoff never renders one.", (ref.id,))


def _target_gate(target: HandoffTarget, policy: HandoffPolicy, add, finding) -> None:
    dialect = policy.sdc_dialect.upper()
    ref = add("TARGET", target.value, "DECLARED", required=True, details={"sdc_dialect": dialect})
    if target == HandoffTarget.FUTURE_VENDOR:
        finding(HandoffIssueSeverity.BLOCKER, "TARGET", "Future vendor target has no implemented adapter compatibility contract.", (ref.id,))
        return
    if dialect == "UNKNOWN" and target != HandoffTarget.GENERIC and not policy.allow_unknown_sdc_dialect:
        finding(HandoffIssueSeverity.BLOCKER, "TARGET", "Target SDC dialect is unknown; it cannot be assumed compatible.", (ref.id,))
    elif dialect not in _TARGET_DIALECTS[target] and not (dialect == "UNKNOWN" and policy.allow_unknown_sdc_dialect):
        finding(HandoffIssueSeverity.BLOCKER, "TARGET", f"SDC dialect {dialect} is incompatible with {target.value}.", (ref.id,))


def _target(value: HandoffTarget | str) -> HandoffTarget:
    try:
        return value if isinstance(value, HandoffTarget) else HandoffTarget(str(value).upper())
    except ValueError as exc:
        raise HandoffError(f"Unsupported handoff target: {value!r}") from exc


def _artifact(value: Any) -> HandoffArtifact:
    return HandoffArtifact(id=str(value.id), kind=str(value.kind), relative_path=str(value.relative_path),
                           sha256=str(value.sha256), size_bytes=int(value.size_bytes), required=bool(value.required))


def _identity(package_id: str, release_id: str, ucm: str, semantic: str, scope: HandoffScope,
              target: HandoffTarget, policy: HandoffPolicy, artifacts: tuple[HandoffArtifact, ...],
              configuration_identity: str) -> HandoffIdentity:
    scope_identity = stable_hash(scope.to_dict())
    identity = stable_hash({
        "package_id": package_id, "release_id": release_id, "ucm_content_identity": ucm,
        "ucm_semantic_identity": semantic, "scope": scope.to_dict(), "target": target.value,
        "policy": policy.to_dict(), "artifacts": [item.to_dict() for item in sorted(artifacts, key=lambda item: item.id)],
        "configuration_identity": configuration_identity,
    })
    return HandoffIdentity("HID-" + identity[:20], package_id, release_id, ucm, semantic, scope_identity, configuration_identity)


def _handoff_id(identity: HandoffIdentity, target: HandoffTarget, policy: HandoffPolicy,
                artifacts: tuple[HandoffArtifact, ...], dependencies: list[HandoffDependency] | tuple[HandoffDependency, ...],
                issues: list[HandoffIssue] | tuple[HandoffIssue, ...]) -> str:
    return "HOF-" + stable_hash({
        "identity": identity.to_dict(), "target": target.value, "policy": policy.to_dict(),
        "artifacts": [item.to_dict() for item in sorted(artifacts, key=lambda item: item.id)],
        "dependencies": [item.to_dict() for item in sorted(dependencies, key=lambda item: (item.kind, item.identity))],
        "issues": [item.to_dict() for item in _sorted_issues(issues)],
    })[:20]


def _evidence(category: str, reference: str, status: str, *, required: bool,
              details: Mapping[str, Any] | None = None) -> HandoffEvidence:
    payload = {"category": category, "reference_id": reference, "status": status, "required": required,
               "details": dict(details or {})}
    return HandoffEvidence("HVE-" + stable_hash(payload)[:20], category, reference, status, required, dict(details or {}))


def _issue(severity: HandoffIssueSeverity, category: str, message: str,
           evidence_ids: tuple[str, ...] = ()) -> HandoffIssue:
    refs = tuple(sorted(set(evidence_ids)))
    return HandoffIssue("HIS-" + stable_hash({"severity": severity.value, "category": category,
                                               "message": message, "evidence_ids": list(refs)})[:20],
                        severity, category, message, refs)


def _identity_value(value: Any | None) -> str:
    if value is None:
        return ""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif hasattr(value, "to_dict"):
        value = value.to_dict()
    elif hasattr(value, "as_dict"):
        value = value.as_dict()
    return stable_hash(value)


def _safe_path(root: Path, relative: str) -> Path | None:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path


def _sorted_issues(values: list[HandoffIssue] | tuple[HandoffIssue, ...]) -> list[HandoffIssue]:
    rank = {HandoffIssueSeverity.BLOCKER: 0, HandoffIssueSeverity.WARNING: 1,
            HandoffIssueSeverity.INFORMATION: 2}
    return sorted(values, key=lambda item: (rank[item.severity], item.category, item.id))


def _failure_message(assessment: HandoffAssessment) -> str:
    messages = [item.message for item in assessment.issues if item.severity == HandoffIssueSeverity.BLOCKER]
    return "Cannot prepare handoff: " + (" ".join(messages) or "handoff is not ready.")


__all__ = [
    "ConstraintHandoffEngine", "HandoffError", "assess_constraint_handoff", "execute_constraint_handoff",
    "prepare_constraint_handoff", "verify_constraint_handoff",
]
